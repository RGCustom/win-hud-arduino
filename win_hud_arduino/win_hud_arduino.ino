/*
  win_hud_arduino.ino

  Прошивка Arduino Pro Micro (ATmega32u4 / Leonardo-совместимая) для проекта
  win-hud-arduino. Плата НИЧЕГО не считает - только:
    - раскладывает уже готовые цвета диодов (пришедшие по serial) по своей
      физической WS2812-ленте через LED_MAP (см. калибровку ниже);
    - печатает три готовые строки на OLED;
    - шлёт хосту события энкодера громкости (вращение/клик).

  Вся логика (градиенты, метрики, шаблоны экранов, OSD громкости, звук,
  раскладка клавиатуры) - на хосте (pc_hud.py). Это тот же принцип, что и в
  проекте shkaf-hud, только тут ОДНА лента переменной длины вместо 4 баров,
  и протокол стал двусторонним.

  ---- Входящий протокол (хост -> плата), см. protocol.py ----
    BAR:<N*6 hex>   - цвет каждого диода, RRGGBB подряд без разделителей
    BRI:<0-100>     - яркость ленты
    CON:<0-255>     - контраст OLED
    L1:<text>       - строка 1 OLED (UTF-8)
    L2:<text>       - строка 2 OLED
    L3:<text>       - строка 3 OLED
    Поля разделены '|', в одной строке может быть любое подмножество полей
    (шлётся только то, что изменилось - см. protocol.ProtocolState).
    CAL             - отдельная команда (не key:value) - запускает калибровку.

  ---- Исходящий протокол (плата -> хост), НОВОЕ относительно shkaf-hud ----
    ENC:<+N|-N>     - энкодер повернули на N "кликов" с прошлой отправки
    BTN:CLICK       - клик кнопки энкодера

  Библиотеки (Arduino IDE -> Library Manager):
    - FastLED
    - U8g2

  ВАЖНО: NUM_LEDS ниже ДОЛЖЕН совпадать с leds_count в /settings хоста -
  это не синхронизируется автоматически, при смене длины ленты нужно
  поправить константу тут, перепрошить И обновить настройку в вебе (иначе
  либо часть ленты останется без данных, либо хвост BAR-строки будет
  проигнорирован).
*/

#include <FastLED.h>
#include <U8g2lib.h>
#include <Wire.h>

// ---------------- КОНФИГ ЖЕЛЕЗА ----------------

#define NUM_LEDS        30      // должно совпадать с cfg["leds_count"] в settings.json хоста
#define LED_PIN         6

// ВАЖНО: CLK обязан сидеть на пине, поддерживающем attachInterrupt() - на
// Pro Micro/Leonardo (ATmega32u4) это ТОЛЬКО пины 0, 1, 2, 3, 7. Пины 0/1
// заняты Serial (связь с хостом), 2/3 - I2C (OLED) - единственный свободный
// вариант это пин 7. Если поставить CLK на любой другой пин (например 8) -
// attachInterrupt() тихо получит NOT_AN_INTERRUPT, encoderISR() не будет
// вызываться НИКОГДА и вращение энкодера не будет регистрироваться вообще,
// независимо от того, как физически подключены DT/CLK - это не ошибка
// проводки, это ограничение конкретных пинов на этой плате.
#define ENCODER_CLK_PIN 7       // энкодер: CLK (A) - ДОЛЖЕН быть interrupt-пином
#define ENCODER_DT_PIN  9       // энкодер: DT (B) - обычный digitalRead, прерывание не нужно
#define ENCODER_BTN_PIN 10      // кнопка энкодера - INPUT_PULLUP, замыкание на GND

#define OLED_FONT_SIZE  1       // 0-4, см. oledFont() ниже - подбирается под физический размер экрана/вкус (0 - самый мелкий)

// Worst-case строка: "BAR:" + NUM_LEDS*6 + "|BRI:100|CON:255" + 3 строки OLED.
// При NUM_LEDS=30 это ~290 байт - берём с запасом. Если увеличишь NUM_LEDS
// сильно (сотня+ диодов) - пересчитай и подними это число (см. заметку в
// protocol.py про SERIAL_BUF_SIZE).
#define SERIAL_BUF_SIZE 600

#define OLED_WIDTH_PX   128
#define OLED_LINE_Y0    14
#define OLED_LINE_Y1    34
#define OLED_LINE_Y2    54
#define OLED_SCROLL_STEP_PX     2
#define OLED_SCROLL_INTERVAL_MS 60
#define OLED_SCROLL_GAP_PX      20   // пробел между концом строки и её повтором при скролле

#define ENCODER_FLUSH_INTERVAL_MS 30   // как часто слать накопленный ENC: хосту
#define BUTTON_DEBOUNCE_MS 40

// ---------------- LED_MAP: калибровка физического порядка диодов ----------------
// Индекс массива - логический номер (0 = "начало" ленты в терминах
// ledbar.py), значение - физический номер диода в цепочке WS2812.
// Дефолт ниже - identity-заглушка (логический == физический), для реальной
// ленты почти наверняка потребуется калибровка: пришли "CAL" в Serial
// Monitor - диоды по одному загорятся белым с номером в консоли, заполни
// массив по факту того, что видишь на ленте, перепрошей.
uint8_t LED_MAP[NUM_LEDS] = {
   0,  1,  2,  3,  4,  5,  6,  7,  8,  9,
  10, 11, 12, 13, 14, 15, 16, 17, 18, 19,
  20, 21, 22, 23, 24, 25, 26, 27, 28, 29,
};

CRGB leds[NUM_LEDS];

// ---------------- OLED (U8g2, кириллица, постраничный режим - экономит RAM) ----------------

U8G2_SSD1306_128X64_NONAME_1_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE);

const uint8_t *oledFont() {
  switch (OLED_FONT_SIZE) {
    case 0: return u8g2_font_6x12_t_cyrillic;    // экстра-мелкий
    case 1: return u8g2_font_6x13_t_cyrillic;
    case 3: return u8g2_font_8x13_t_cyrillic;
    case 4: return u8g2_font_9x15_t_cyrillic;
    default: return u8g2_font_10x20_t_cyrillic;  // case 2 / фолбэк
  }
}

String oledLines[3] = {"", "", ""};
int16_t scrollOffset[3] = {0, 0, 0};
unsigned long lastScrollMs = 0;

// oledDirty - НОВОЕ: раньше drawOled() (несколько проходов по I2C на
// однобуферном "_1_" конструкторе U8g2) вызывался БЕЗУСЛОВНО на каждой
// итерации loop(), даже если экран не менялся ни на пиксель. Это ощутимо
// тормозило частоту loop() и, как следствие, частоту pollButton() ниже -
// если целый клик (нажал-отпустил) укладывался в один "медленный" проход
// отрисовки, pollButton() мог просто не увидеть переход состояния между
// двумя своими опросами (быстрые клики "терялись", хотя сам debounce
// был в порядке). Теперь перерисовываем OLED, только когда есть что
// перерисовывать - см. флаг выставляется в applyOledLine() (новый текст)
// и в updateScroll() (реальный шаг скролла), сбрасывается после drawOled().
bool oledDirty = true;

// ---------------- serial: входящий буфер ----------------

char serialBuf[SERIAL_BUF_SIZE];
uint16_t serialBufLen = 0;

// ---------------- энкодер: состояние ----------------

volatile int16_t encoderDelta = 0;   // накопленные "клики" с прошлой отправки хосту
volatile uint8_t lastEncoderState = 0;
unsigned long lastEncoderFlushMs = 0;

bool lastButtonState = HIGH;
unsigned long lastButtonChangeMs = 0;
bool buttonDebounced = HIGH;

// ---------------- вспомогательные функции протокола ----------------

// Разбирает 2 hex-символа в число 0-255. Некорректный ввод -> 0 (не падаем
// на мусоре/оборванной строке - см. общий принцип проекта: serial-шум это
// норма, а не повод виснуть).
uint8_t hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return 0;
}

uint8_t hexByte(const char *s) {
  return (hexNibble(s[0]) << 4) | hexNibble(s[1]);
}

// Раскладывает N*6 hex-символов из value в leds[] через LED_MAP. Если value
// короче/длиннее NUM_LEDS*6 - берём min(), лишнее/недостающее игнорируем
// (не должно происходить при живом хосте, но лучше отрисовать частично, чем
// упасть).
void applyBarValue(const char *value, uint16_t valueLen) {
  uint16_t count = valueLen / 6;
  if (count > NUM_LEDS) count = NUM_LEDS;
  for (uint16_t i = 0; i < count; i++) {
    const char *px = value + i * 6;
    uint8_t r = hexByte(px);
    uint8_t g = hexByte(px + 2);
    uint8_t b = hexByte(px + 4);
    uint8_t physIdx = LED_MAP[i];
    if (physIdx < NUM_LEDS) {
      leds[physIdx] = CRGB(r, g, b);
    }
  }
}

void applyBrightness(const char *value) {
  int pct = atoi(value);
  pct = constrain(pct, 0, 100);
  FastLED.setBrightness(map(pct, 0, 100, 0, 255));
}

void applyContrast(const char *value) {
  int v = atoi(value);
  v = constrain(v, 0, 255);
  u8g2.setContrast(v);
}

void applyOledLine(uint8_t idx, const char *value) {
  oledLines[idx] = String(value);
  scrollOffset[idx] = 0;  // новая строка - скролл начинается заново
  oledDirty = true;
}

void runCalibration();  // объявление вперёд - используется в processCommandLine ниже

// Разбирает одну command-строку вида "KEY:value|KEY2:value2|..." (без \n).
// Пустые/битые токены пропускаются молча - см. общий принцип "serial-мусор
// не повод падать" (тот же, что и в protocol.parse_incoming_line на хосте).
void processCommandLine(char *line) {
  if (strcmp(line, "CAL") == 0) {
    runCalibration();
    return;
  }

  char *token = strtok(line, "|");
  while (token != NULL) {
    char *colon = strchr(token, ':');
    if (colon != NULL) {
      *colon = '\0';
      const char *key = token;
      const char *value = colon + 1;
      uint16_t valueLen = strlen(value);

      if (strcmp(key, "BAR") == 0) {
        applyBarValue(value, valueLen);
      } else if (strcmp(key, "BRI") == 0) {
        applyBrightness(value);
      } else if (strcmp(key, "CON") == 0) {
        applyContrast(value);
      } else if (strcmp(key, "L1") == 0) {
        applyOledLine(0, value);
      } else if (strcmp(key, "L2") == 0) {
        applyOledLine(1, value);
      } else if (strcmp(key, "L3") == 0) {
        applyOledLine(2, value);
      }
      // неизвестный key - молча игнорируем (совместимость вперёд, если
      // хост когда-нибудь пришлёт новое поле, а прошивка ещё старая)
    }
    token = strtok(NULL, "|");
  }

  FastLED.show();
}

// ---------------- калибровка ленты (команда CAL) ----------------

void runCalibration() {
  Serial.println(F("=== CAL: калибровка LED_MAP ==="));
  Serial.println(F("Диоды загорятся по одному белым, номер - в консоли."));
  Serial.println(F("Запиши физический порядок и вручную заполни LED_MAP в .ino."));
  for (uint16_t i = 0; i < NUM_LEDS; i++) {
    FastLED.clear();
    leds[i] = CRGB(255, 255, 255);
    FastLED.show();
    Serial.print(F("Физический диод #"));
    Serial.println(i);
    delay(600);
  }
  FastLED.clear();
  FastLED.show();
  Serial.println(F("=== CAL: готово ==="));
}

// ---------------- энкодер (прерывание на CLK) ----------------

void encoderISR() {
  // Простой квадратурный декодер: направление определяется состоянием DT
  // в момент фронта на CLK. Дребезг тут не фильтруем аппаратно - большинство
  // модулей энкодера уже имеют конденсаторы на плате; если дребезжит -
  // первое, что стоит проверить - именно железо/конденсаторы, а не эту
  // прошивку.
  bool clkState = digitalRead(ENCODER_CLK_PIN);
  if (clkState != lastEncoderState) {
    if (digitalRead(ENCODER_DT_PIN) != clkState) {
      encoderDelta++;
    } else {
      encoderDelta--;
    }
  }
  lastEncoderState = clkState;
}

void pollButton() {
  bool raw = digitalRead(ENCODER_BTN_PIN);
  unsigned long now = millis();
  if (raw != lastButtonState) {
    lastButtonChangeMs = now;
    lastButtonState = raw;
  }
  if (now - lastButtonChangeMs > BUTTON_DEBOUNCE_MS && buttonDebounced != lastButtonState) {
    buttonDebounced = lastButtonState;
    if (buttonDebounced == LOW) {  // нажатие - активный уровень LOW (INPUT_PULLUP)
      Serial.println(F("BTN:CLICK"));
    }
  }
}

void flushEncoder() {
  unsigned long now = millis();
  if (now - lastEncoderFlushMs < ENCODER_FLUSH_INTERVAL_MS) return;
  lastEncoderFlushMs = now;

  noInterrupts();
  int16_t delta = encoderDelta;
  encoderDelta = 0;
  interrupts();

  if (delta != 0) {
    Serial.print(F("ENC:"));
    if (delta > 0) Serial.print('+');
    Serial.println(delta);
  }
}

// ---------------- OLED: отрисовка со скроллом длинных строк ----------------

void drawOled() {
  u8g2.firstPage();
  do {
    u8g2.setFont(oledFont());
    const int16_t yPos[3] = {OLED_LINE_Y0, OLED_LINE_Y1, OLED_LINE_Y2};
    for (uint8_t i = 0; i < 3; i++) {
      if (oledLines[i].length() == 0) continue;
      int16_t textWidth = u8g2.getUTF8Width(oledLines[i].c_str());
      if (textWidth <= OLED_WIDTH_PX) {
        // короткая строка - печатаем статично, без скролла
        u8g2.drawUTF8(0, yPos[i], oledLines[i].c_str());
      } else {
        // длинная строка - скроллим влево, зацикливая через OLED_SCROLL_GAP_PX
        int16_t x = -scrollOffset[i];
        u8g2.drawUTF8(x, yPos[i], oledLines[i].c_str());
        u8g2.drawUTF8(x + textWidth + OLED_SCROLL_GAP_PX, yPos[i], oledLines[i].c_str());
      }
    }
  } while (u8g2.nextPage());
}

void updateScroll() {
  unsigned long now = millis();
  if (now - lastScrollMs < OLED_SCROLL_INTERVAL_MS) return;
  lastScrollMs = now;

  for (uint8_t i = 0; i < 3; i++) {
    if (oledLines[i].length() == 0) continue;
    int16_t textWidth = u8g2.getUTF8Width(oledLines[i].c_str());
    if (textWidth <= OLED_WIDTH_PX) {
      scrollOffset[i] = 0;
      continue;
    }
    scrollOffset[i] += OLED_SCROLL_STEP_PX;
    if (scrollOffset[i] >= textWidth + OLED_SCROLL_GAP_PX) {
      scrollOffset[i] = 0;
    }
    oledDirty = true;
  }
}

// ---------------- setup / loop ----------------

void setup() {
  Serial.begin(115200);

  FastLED.addLeds<WS2812B, LED_PIN, GRB>(leds, NUM_LEDS);
  FastLED.setBrightness(38);  // ~15% - стартовое значение до первого BRI: от хоста
  FastLED.clear();
  FastLED.show();

  u8g2.begin();
  u8g2.setContrast(255);

  pinMode(ENCODER_CLK_PIN, INPUT_PULLUP);
  pinMode(ENCODER_DT_PIN, INPUT_PULLUP);
  pinMode(ENCODER_BTN_PIN, INPUT_PULLUP);
  lastEncoderState = digitalRead(ENCODER_CLK_PIN);
  attachInterrupt(digitalPinToInterrupt(ENCODER_CLK_PIN), encoderISR, CHANGE);

  lastButtonState = digitalRead(ENCODER_BTN_PIN);
  buttonDebounced = lastButtonState;
}

void loop() {
  // ---- читаем serial построчно (до \n), не блокируясь ----
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n') {
      if (serialBufLen > 0) {
        serialBuf[serialBufLen] = '\0';
        processCommandLine(serialBuf);
        serialBufLen = 0;
      }
    } else if (c != '\r') {
      if (serialBufLen < SERIAL_BUF_SIZE - 1) {
        serialBuf[serialBufLen++] = c;
      } else {
        // строка длиннее буфера - переполнение, сбрасываем накопленное,
        // чтобы не собрать "гибрид" из двух команд подряд
        serialBufLen = 0;
      }
    }
  }

  pollButton();
  flushEncoder();

  updateScroll();
  if (oledDirty) {
    drawOled();
    oledDirty = false;
  }
}
