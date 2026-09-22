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

    Внутри L1/L2/L3 отдельно кодируются мини-графики (спарклайны CPU/RAM/
    GPU/... - см. history.py/variables.py на хосте): байты со значением
    0x01-0x08 (8 уровней высоты) - это НЕ текст, а "нарисуй столбик такой-то
    высоты" - см. drawLineBars() ниже. Эти значения физически не встречаются
    в обычном ASCII/UTF-8/кириллическом тексте, поэтому отдельный маркер
    начала/конца не нужен - прошивка сама переключается между "печатать
    текст" и "рисовать столбик" по диапазону байта, символ за символом.
    0x00 в этот диапазон НЕ входит НАМЕРЕННО - это terminator C-строки
    (см. serialBuf/processCommandLine ниже), использовать его как код
    столбика было бы опасно.

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

  WATCHDOG ОТСУТСТВИЯ ДАННЫХ ОТ ХОСТА: если ПК выключили/pc_hud.py закрыли -
  serial-порт с той стороны просто перестаёт слать что-либо, а плата САМА ПО
  СЕБЕ никак об этом не узнаёт и без специальной проверки держала бы ПОСЛЕДНИЙ
  полученный кадр (лента+OLED) вечно. См. NO_DATA_TIMEOUT_MS/checkHostTimeout()
  ниже - если за NO_DATA_TIMEOUT_MS (3 минуты) не пришло НИ ОДНОЙ command-строки,
  лента гасится, OLED очищается - и остаются погашенными, пока не придут новые
  данные (никакого дополнительного подтверждения на возврат не нужно - первая
  же пришедшая BAR:/L1-3: команда перезапишет содержимое сама).

  "РАЗБУДИТЬ" ХОСТА КЛИКОМ ЭНКОДЕРА (см. attemptWakeHost() ниже): пока
  hostTimedOut==true, клик кнопки энкодера НЕ шлёт BTN:CLICK по serial (слать
  некому - хост не читает порт) - вместо этого плата сама представляется
  Windows USB HID-клавиатурой (Keyboard.h, штатно доступна на 32u4/Leonardo)
  и шлёт одно короткое нажатие Left Ctrl. Left Ctrl выбран специально - это
  модификатор, он ничего не печатает, даже если случайно попадёт в активное
  текстовое поле после пробуждения.

  ВАЖНО - что это реально может и не может разбудить (ограничение
  железа/BIOS/Windows, не прошивки):
    - Сон (Sleep/S3)              - работает практически всегда "из коробки".
    - Полное выключение (Shutdown/S5) - работает, ТОЛЬКО если в BIOS
      материнки явно включена опция вида "Power On By Keyboard/USB" (у
      разных производителей называется по-разному) - без неё чипсет в S5
      просто не слушает USB, и это никак не обойти со стороны прошивки.
    - Зависание Windows / самопроизвольная перезагрузка - НЕ поможет и не
      должно: это не про "включение", тут вмешиваться нечем.
  Плата физически остаётся запитанной по USB даже в этих состояниях - иначе
  сам watchdog выше не мог бы гасить ленту/OLED - поэтому USB HID в принципе
  долетает до хоста, вопрос только в том, слушает ли его в данный момент
  конкретная материнка/ОС.
*/

#include <FastLED.h>
#include <U8g2lib.h>
#include <Wire.h>
#include <Keyboard.h>

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

// Высота столбика мини-графика (см. drawLineBars() ниже) в пикселях -
// растёт вверх ОТ baseline строки (та же Y-координата, что раньше шла
// напрямую в drawUTF8()), поэтому не должна быть больше межстрочного
// интервала (OLED_LINE_Y1-OLED_LINE_Y0 = 20px выше) минус запас на нижние
// выносные элементы соседних глифов (хвостики у "р"/"у" и т.п.) - 10px с
// таким запасом работает при любом из шрифтов oledFont() ниже.
#define GRAPH_BAR_MAX_HEIGHT_PX 10

#define ENCODER_FLUSH_INTERVAL_MS 30   // как часто слать накопленный ENC: хосту
#define BUTTON_DEBOUNCE_MS 40

// Сколько миллисекунд ждать ХОТЬ ОДНУ команду от хоста (BAR:/BRI:/CON:/L1-3:),
// прежде чем считать хост "пропавшим" (выключили ПК/закрыли pc_hud.py/выдернули
// USB-хаб и т.п.) и погасить ленту+OLED - иначе плата держит ПОСЛЕДНИЙ присланный
// кадр вечно, т.к. сама по себе ничего не знает о состоянии хоста. 3 минуты -
// с большим запасом относительно protocol.FULL_RESYNC_SECONDS=30с на хосте
// (полный ресинк шлётся туда каждые 30с ДАЖЕ если ничего не изменилось - см.
// ProtocolState.build() в protocol.py), поэтому пока pc_hud.py жив и serial
// открыт, эта отметка обновляется минимум раз в 30с и таймаут никогда не
// сработает при нормальной работе - сработает только при реальном пропадании
// хоста. Не настройка в /settings (не переменная в web UI) - в отличие от
// tick_interval/leds_count, менять это на лету незачем, а завести отдельный
// протокольный канал ради этого избыточно; значение просто правится тут же
// перед перепрошивкой, как и OLED_SCROLL_STEP_PX/BUTTON_DEBOUNCE_MS выше.
#define NO_DATA_TIMEOUT_MS 45000UL

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

// Ширина одной "ячейки" столбика графика (см. drawLineBars() ниже), px -
// ПЕРВОЕ число в имени шрифта из oledFont() выше (шрифты u8g2 названы как
// "<ширина>x<высота>", моноширинные для латиницы/цифр) - держим её здесь
// синхронно с OLED_FONT_SIZE вручную (не читаем из u8g2 программно - метод
// вроде getMaxCharWidth() существует, но у транспарентных "_t_" шрифтов
// возвращает не то, что нужно для моноширинной раскладки цифр/латиницы;
// проще и надёжнее явное соответствие таблице выше). Столбики выравниваются
// по той же сетке, что и текст - на хосте {cpu_graph:8} и так уже думает в
// "символах", не пикселях (см. history.py/templates.py), поэтому колонка
// столбика ДОЛЖНА совпадать по ширине с колонкой обычного символа.
uint8_t oledCharWidthPx() {
  switch (OLED_FONT_SIZE) {
    case 0: return 6;
    case 1: return 6;
    case 3: return 8;
    case 4: return 9;
    default: return 10;  // case 2
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

// ---------------- watchdog отсутствия данных от хоста ----------------

// lastHostDataMs - millis() момента последней УСПЕШНО РАЗОБРАННОЙ command-строки
// от хоста (обновляется в loop() перед вызовом processCommandLine(), см. ниже) -
// НЕ обновляется на ENC:/BTN:, это исходящие сообщения ПЛАТЫ, а не входящие от
// хоста, и сами по себе не подтверждают, что хост жив. Инициализация в 0 (а не
// millis() из setup()) осознанная - если хост так и не подключился ни разу за
// первые NO_DATA_TIMEOUT_MS после включения платы, это тоже "нет данных", а не
// особый случай (лента и так пуста после сброса, разницы в поведении не видно,
// зато не нужно тащить флаг "было подключение хоть раз").
unsigned long lastHostDataMs = 0;
bool hostTimedOut = false;   // true - лента/OLED уже погашены watchdog'ом, ждём новых данных

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

// Проверяет, не пропал ли хост (см. NO_DATA_TIMEOUT_MS/lastHostDataMs выше) -
// вызывается из loop() КАЖДУЮ итерацию, но реально гасит ленту/OLED только
// ОДИН РАЗ при переходе в состояние "хост пропал" (флаг hostTimedOut защищает
// от повторного FastLED.show()/oledDirty на каждой итерации, пока хост так и
// не вернулся - иначе бессмысленная нагрузка на I2C/SPI впустую). Обратный
// переход ("хост вернулся") НЕ обрабатывается тут отдельной веткой - как
// только придёт первая же валидная command-строка, loop() обновит
// lastHostDataMs И applyBarValue()/applyOledLine() сами перезапишут
// leds[]/oledLines[] новым содержимым - специально сбрасывать hostTimedOut
// в false можно было бы и тут, но проще и надёжнее сделать это ровно там же,
// где обновляется lastHostDataMs (см. loop()), одним местом, а не размазывать
// по двум функциям.
void checkHostTimeout() {
  if (hostTimedOut) return;
  if (millis() - lastHostDataMs < NO_DATA_TIMEOUT_MS) return;

  hostTimedOut = true;

  FastLED.clear();
  FastLED.show();

  oledLines[0] = "";
  oledLines[1] = "";
  oledLines[2] = "";
  oledDirty = true;
}

// ---------------- "разбудить" хоста через USB HID (см. докстринг модуля) ----------------

// Минимальный интервал между попытками - защита от повторных срабатываний
// при удержании/частых кликах кнопки, пока хост ещё не откликнулся (первая
// же валидная command-строка от хоста сбросит hostTimedOut в loop(), тогда
// pollButton() снова пойдёт по обычной ветке BTN:CLICK, а не сюда).
#define WAKE_KEY_COOLDOWN_MS 5000UL
unsigned long lastWakeAttemptMs = 0;

void attemptWakeHost() {
  unsigned long now = millis();
  // lastWakeAttemptMs == 0 - ещё не было ни одной попытки, кулдаун не
  // применяется (иначе первый клик после старта платы пришлось бы ждать
  // WAKE_KEY_COOLDOWN_MS от millis()==0, чего на практике не случится, но
  // явная проверка понятнее неявного совпадения).
  if (lastWakeAttemptMs != 0 && now - lastWakeAttemptMs < WAKE_KEY_COOLDOWN_MS) return;
  lastWakeAttemptMs = now;

  Keyboard.press(KEY_LEFT_CTRL);
  delay(15);  // достаточно для регистрации нажатия хостом, короче незаметно для пользователя
  Keyboard.release(KEY_LEFT_CTRL);

  // Визуальное подтверждение "клик принят, попытка ушла" - обычным путём
  // (через OLED/ленту от хоста) подтвердить нечего, хоста ещё нет. Короткая
  // синяя вспышка всей ленты - checkHostTimeout() не будет с ней бороться
  // (он ничего не делает повторно, пока hostTimedOut уже true, см. его
  // докстринг), а следующий пришедший от хоста BAR: сам перезапишет ленту
  // как обычно.
  fill_solid(leds, NUM_LEDS, CRGB(0, 120, 255));
  FastLED.show();
  delay(200);
  FastLED.clear();
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
      if (hostTimedOut) {
        // Хост не читает serial (см. checkHostTimeout()) - обычный
        // BTN:CLICK слать некому, вместо этого пробуем разбудить его через
        // USB HID (см. attemptWakeHost() и докстринг модуля за подробностями
        // и ограничениями по Sleep/Shutdown/BIOS).
        attemptWakeHost();
      } else {
        Serial.println(F("BTN:CLICK"));
      }
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

// ---------------- OLED: столбики графика внутри строки (см. докстринг ----
// ---------------- модуля "Входящий протокол" за форматом) ----------------

// Замеряет ширину строки в пикселях, УЧИТЫВАЯ столбики графика (control-
// байты 0x01-0x08) - НЕ рисует ничего, только считает. Нужна отдельно от
// drawLineBars() ниже (которая и рисует, и попутно тоже могла бы вернуть
// ширину), потому что updateScroll() должен знать ширину строки КАЖДЫЙ
// вызов (60мс, см. OLED_SCROLL_INTERVAL_MS), а рисовать в этот момент
// ничего не нужно - drawOled() и так перерисует буфер отдельно, только
// когда oledDirty (см. loop() ниже за экономией на этом).
int16_t lineWidthPx(const char *line) {
  const uint8_t *p = (const uint8_t *)line;
  int16_t width = 0;
  uint8_t cellW = oledCharWidthPx();

  while (*p) {
    if (*p >= 1 && *p <= 8) {
      while (*p >= 1 && *p <= 8) {
        width += cellW;
        p++;
      }
    } else {
      // Кусок обычного текста между двумя прогонами столбиков (либо вся
      // строка целиком, если столбиков в ней нет) - до следующего
      // control-байта 1-8 или конца строки. 40 байт с большим запасом
      // относительно того, что реально шлёт хост в ОДНОМ текстовом куске
      // одной OLED-строки (см. _center_line(width=16) в osd.py на хосте -
      // весь дизайн шаблонов рассчитан на 16-символьную сетку) - НЕ делаем
      // буфер большим "на всякий случай": это стек ATmega32u4 (2.5KB RAM
      // всего), а lineWidthPx()/drawLineBars() вызываются из u8g2-цикла
      // firstPage()/nextPage(), не рекурсивно, но с обычным вызовом на
      // каждую из 3 строк за кадр - раздувать локальный буфер тут дорого.
      char buf[40];
      uint16_t n = 0;
      while (*p && !(*p >= 1 && *p <= 8) && n < sizeof(buf) - 1) {
        buf[n++] = *p++;
      }
      buf[n] = '\0';
      width += u8g2.getUTF8Width(buf);
    }
  }
  return width;
}

// Печатает ОДНУ OLED-строку начиная с колонки x0, поддерживая столбики
// графика ВНУТРИ текста - см. докстринг модуля "Входящий протокол" за
// форматом control-байтов. Обычный текст печатается как раньше, через
// u8g2.drawUTF8() (она уже умеет многобайтовый UTF-8 - см. ⚠/↓/↑ в
// дефолтных экранах на хосте, кириллицу через шрифты *_cyrillic - здесь
// ничего не меняется, просто вызывается кусками между столбиками, а не на
// всю строку разом). baseline_y - та же Y-координата, что раньше шла
// напрямую в drawUTF8() из drawOled() (см. ниже) - столбик растёт ВВЕРХ от
// неё на высоту level/8 * GRAPH_BAR_MAX_HEIGHT_PX.
void drawLineBars(int16_t x0, int16_t baseline_y, const char *line) {
  const uint8_t *p = (const uint8_t *)line;
  int16_t x = x0;
  uint8_t cellW = oledCharWidthPx();

  while (*p) {
    if (*p >= 1 && *p <= 8) {
      while (*p >= 1 && *p <= 8) {
        uint8_t h = ((uint16_t)(*p) * GRAPH_BAR_MAX_HEIGHT_PX) / 8;
        if (h > 0) {
          // Столбик по центру своей ячейки (не на всю ширину cellW) -
          // визуально ближе к классическому спарклайну (тонкие штрихи), чем
          // сплошная заливка "стена к стене"; drawVLine(x,y,h) рисует ВНИЗ
          // от y, поэтому верхний край считаем от baseline_y так, чтобы
          // низ столбика лёг ровно на baseline (как и текст рядом).
          u8g2.drawBox(x, baseline_y - h + 1, cellW - 1, h);
        }
        x += cellW;
        p++;
      }
    } else {
      char buf[40];  // см. обоснование размера в lineWidthPx() выше
      uint16_t n = 0;
      while (*p && !(*p >= 1 && *p <= 8) && n < sizeof(buf) - 1) {
        buf[n++] = *p++;
      }
      buf[n] = '\0';
      u8g2.drawUTF8(x, baseline_y, buf);
      x += u8g2.getUTF8Width(buf);
    }
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
      int16_t textWidth = lineWidthPx(oledLines[i].c_str());
      if (textWidth <= OLED_WIDTH_PX) {
        // короткая строка - печатаем статично, без скролла
        drawLineBars(0, yPos[i], oledLines[i].c_str());
      } else {
        // длинная строка - скроллим влево, зацикливая через OLED_SCROLL_GAP_PX
        int16_t x = -scrollOffset[i];
        drawLineBars(x, yPos[i], oledLines[i].c_str());
        drawLineBars(x + textWidth + OLED_SCROLL_GAP_PX, yPos[i], oledLines[i].c_str());
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
    int16_t textWidth = lineWidthPx(oledLines[i].c_str());
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

  Keyboard.begin();  // USB HID-клавиатура (см. attemptWakeHost()) - composite
                      // с уже поднятым Serial CDC, ничего дополнительно
                      // настраивать не нужно на 32u4/Leonardo
}

void loop() {
  // ---- читаем serial построчно (до \n), не блокируясь ----
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n') {
      if (serialBufLen > 0) {
        serialBuf[serialBufLen] = '\0';
        // Хост прислал хоть что-то - считаем его живым и сбрасываем watchdog
        // ЗДЕСЬ, а не внутри checkHostTimeout()/processCommandLine() - это
        // единственное место, где действительно известно "от хоста только что
        // пришла целая строка" (CAL из Serial Monitor тоже считается - это
        // тоже говорит о том, что порт живой и с той стороны кто-то есть).
        lastHostDataMs = millis();
        hostTimedOut = false;
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
  checkHostTimeout();

  updateScroll();
  if (oledDirty) {
    drawOled();
    oledDirty = false;
  }
}
