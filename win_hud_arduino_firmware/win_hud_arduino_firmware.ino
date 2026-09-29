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
WATCHDOG ОТСУТСТВИЯ ДАННЫХ ОТ ХОСТА: если ПК выключили/pc_hud.py закрыли -
serial-порт с той стороны просто перестаёт слать что-либо, а плата САМА ПО
СЕБЕ никак об этом не узнаёт и без специальной проверки держала бы ПОСЛЕДНИЙ
полученный кадр (лента+OLED) вечно. См. NO_DATA_TIMEOUT_MS/checkHostTimeout()
ниже - если за NO_DATA_TIMEOUT_MS не пришло НИ ОДНОЙ command-строки, лента
гасится, OLED очищается (либо, если включён офлайн-режим - показывается
офлайн-экран, см. ниже) - и остаются в этом состоянии, пока не придут новые
данные (никакого дополнительного подтверждения на возврат не нужно - первая
же пришедшая BAR:/L1-3: команда перезапишет содержимое сама).
Клик кнопки энкодера во время hostTimedOut НИЧЕГО не делает (слать
BTN:CLICK некому - хост не читает порт). Раньше тут была попытка
"разбудить" хост через USB HID-клавиатуру (Keyboard.h) - убрана: на
практике не срабатывала, а композитный USB HID-дескриптор заметно раздувал
и без того небольшую флеш-память ATmega32u4 (32КБ, из которых часть уже
занята бутлоадером).
*/
#include <FastLED.h>
#include <U8g2lib.h>
#include <Wire.h>
#include <stdlib.h>   // strtoul (TSYNC:/OFFCFG:)
#include <string.h>   // strncpy/strchr/strncmp/strlen (уже неявно требовались и раньше, для strtok/atoi)
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
#define OLED_FONT_SIZE  1       // 0-4, см. OLED_FONT_SELECTED ниже - подбирается под физический размер экрана/вкус (0 - самый мелкий)

// ЖЁСТКИЙ выбор шрифта через #if - компилятор физически не сможет включить
// в .hex остальные 4 шрифта, т.к. их имён больше нет в коде. Это экономит
// 6-8 КБ флеша по сравнению с switch-конструкцией (компилятор AVR GCC часто
// не может доказать, что ветки switch недостижимы, и включает ВСЕ шрифты
// "на всякий случай").
#if OLED_FONT_SIZE == 0
  #define OLED_FONT_SELECTED u8g2_font_6x12_t_cyrillic
  #define OLED_CHAR_WIDTH 6
#elif OLED_FONT_SIZE == 1
  #define OLED_FONT_SELECTED u8g2_font_6x13_t_cyrillic
  #define OLED_CHAR_WIDTH 6
#elif OLED_FONT_SIZE == 3
  #define OLED_FONT_SELECTED u8g2_font_8x13_t_cyrillic
  #define OLED_CHAR_WIDTH 8
#elif OLED_FONT_SIZE == 4
  #define OLED_FONT_SELECTED u8g2_font_9x15_t_cyrillic
  #define OLED_CHAR_WIDTH 9
#else
  #define OLED_FONT_SELECTED u8g2_font_10x20_t_cyrillic
  #define OLED_CHAR_WIDTH 10
#endif

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
// таким запасом работает при любом из шрифтов выше.
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
// OfflineToken - объявлена ЗДЕСЬ, в самом верху файла, а НЕ рядом с местом
// использования - Arduino IDE сама генерирует forward-декларации всех
// функций сразу после блока #include/#define, то есть РАНЬШЕ любого места
// дальше в файле; если бы struct был объявлен позже, автосгенерированный
// прототип функции с параметром этого типа ссылался бы на ещё неизвестный
// компилятору тип ("has not been declared").
struct OfflineToken { const char *name; const char *value; };
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

// Ширина одной  "ячейки " столбика графика (см. drawLineBars() ниже), px -
// ПЕРВОЕ число в имени шрифта из OLED_FONT_SELECTED выше (шрифты u8g2 названы как
//  " <ширина >x <высота > ", моноширинные для латиницы/цифр) - держим её здесь
// синхронно с OLED_FONT_SIZE вручную (не читаем из u8g2 программно - метод
// вроде getMaxCharWidth() существует, но у транспа рентных  " t " шрифтов
// возвращает не то, что нужно для моноширинной раскладки цифр/латиницы;
// проще и надёжнее явное соответствие таблице выше). Столбики выравниваются
// по той же сетке,  что и текст - на хосте {cpu_graph:8} и так уже думает в
//  "символах ", не пикселях (см. history.py/templates.py), поэтому колонка
// столбика ДОЛЖНА совпадать по ширине с колонкой обычного символа.

// oledLines - обычные char-массивы вместо класса String. Это экономит
// 1.5-2.5 КБ флеша (компилятор не тянет malloc/free и методы String) и
// 48*3=144 байт RAM (фиксированный массив вместо динамической аллокации).
#define OLED_LINE_MAX_LEN 48
char oledLines[3][OLED_LINE_MAX_LEN] = {"", "", ""};
int16_t scrollOffset[3] = {0, 0, 0};
unsigned long lastScrollMs = 0;
// oledDirty - НОВОЕ: раньше drawOled() (несколько проходов по I2C на
// однобуферном "1" конструкторе U8g2) вызывался БЕЗУСЛОВНО на каждой
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
// ---------------- offline-режим: конфиг (ТОЛЬКО в RAM, без EEPROM) ----------------
// Плата 24/7 подключена к ПК - реальное отключение питания платы бывает
// исключительно редко (аварийное отключение электричества, раз в несколько
// лет - см. обсуждение с Konstantin), поэтому конфиг офлайн-экрана хранится
// ПРОСТО В ОЗУ, с дефолтами при старте: если питание всё же пропадёт, плата
// поднимется с дефолтом (офлайн-режим выключен) и тут же получит актуальный
// конфиг от хоста заново при следующем подключении (см. OFFCFG:/OFFL1-3: в
// processCommandLine() ниже) - никакого EEPROM.h не нужно, экономит и флеш,
// и код (нет магии валидности записи/загрузки/сохранения).
#define OFFLINE_LINE_MAX 40   // шаблон строки; {nightled_status} {nightled_time} вместе = 31 символ
// шаблон может быть чуть длиннее подстрок
struct OfflineConfig {
uint8_t enabled;
uint16_t timeout_s;   // 0 = ещё не приходило от хоста - см. effectiveTimeoutMs()
char l1[OFFLINE_LINE_MAX];
char l2[OFFLINE_LINE_MAX];
char l3[OFFLINE_LINE_MAX];
// ---- "Ночник" (см. обсуждение в чате) - НОВОЕ ----
// button_clock_s: пока хост пропал (hostTimedOut), клик кнопки энкодера
// показывает офлайн-часы на это число секунд - НЕЗАВИСИМО от enabled/
// enabled (см. pollButton()/renderOfflineScreenNow() ниже).
uint16_t button_clock_s;
// led_enabled/led_r/led_g/led_b: пока хост пропал, вращение энкодера
// прибавляет/убавляет минуты подсветки ленты этим сплошным цветом (см.
// updateNightlight()/flushEncoder() ниже) - led_enabled разрешает саму
// возможность (если выключено в /offline - вращение просто ничего не
// делает с лентой, даже если nightlightRemainingMs почему-то не ноль).
uint8_t led_enabled;
uint8_t led_r, led_g, led_b;
};
// Дефолты - "весь день" (start==end) и офлайн-режим выключен, пока хост не
// пришлёт первый OFFCFG: (см. api_offline()/metrics_main_loop в pc_hud.py -
// шлёт конфиг сразу при (пере)подключении, так что в реальности эти
// значения живут доли секунды после старта платы). button_clock_s=60 и
// led_enabled=0 - те же дефолты, что и в pc_hud.py (DEFAULT_OFFLINE_*).
OfflineConfig offlineConfig = {
0, (uint16_t)(NO_DATA_TIMEOUT_MS / 1000UL),
"{time_now}", "{weekday_name} {date_now}", "{uptime}",
60, 0, 0, 0, 0,
};
unsigned long effectiveTimeoutMs() {
// timeout_s == 0 - хост ещё ни разу не присылал OFFCFG: (либо прислал
// мусор) - используем тот же фолбэк, что и раньше (NO_DATA_TIMEOUT_MS).
if (offlineConfig.timeout_s == 0) return NO_DATA_TIMEOUT_MS;
return (unsigned long)offlineConfig.timeout_s * 1000UL;
}
// ---------------- offline-режим: мягкие часы поверх millis() ----------------
// RTC на плате нет - "сейчас" считается от последней синхронизации с хостом
// (TSYNC:, см. processCommandLine() ниже), дальше тикает по millis().
unsigned long epochAtSync = 0;
unsigned long millisAtSync = 0;
bool clockSynced = false;      // false - TSYNC ещё ни разу не приходил (плата
// только что включена) - без синхронизации
// офлайн-экран показывать нельзя, часы
// попросту неизвестны
#define OFFLINE_UPTIME_MAX 20   // "5d 12h"/"48m" и т.п. - см. format_duration()
// на хосте (pc_hud.py), там значения всегда
// короткие; с запасом
char lastUptimeText[OFFLINE_UPTIME_MAX] = "";  // последний UPT: - замороженный
// аптайм Windows для {uptime}
// на офлайн-экране - обычный
// char[], не String: реже
// приходит, heap-аллокация
// тут не нужна вовсе.
// Days-from-civil/civil-from-days по алгоритму Говарда Хиннанта
// (http://howardhinnant.github.io/date_algorithms.html) - целочисленный,
// без float, корректен для любого разумного диапазона дат. Нужен, т.к.
// на плате нет ни time.h с настоящим RTC, ни места под тяжёлую библиотеку
// работы с календарём - тут требуется только epoch -> (год, месяц, день).
void civilFromDays(long z, int &y, int &m, int &d) {
z += 719468L;
long era = (z >= 0 ? z : z - 146096L) / 146097L;
unsigned long doe = (unsigned long)(z - era * 146097L);           // [0, 146096]
unsigned long yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;  // [0, 399]
long yr = (long)yoe + era * 400L;
unsigned long doy = doe - (365 * yoe + yoe / 4 - yoe / 100);       // [0, 365]
unsigned long mp = (5 * doy + 2) / 153;                            // [0, 11]
d = (int)(doy - (153 * mp + 2) / 5 + 1);                           // [1, 31]
m = (int)(mp + (mp < 10 ? 3 : -9));                                 // [1, 12]
y = (int)(yr + (m <= 2 ? 1 : 0));
}
// Текущее "мягкое" время - epochAtSync + прошедшее с синхронизации (в millis(),
// беззнаково - переполнение millis() через ~49 дней корректно самокомпенсируется
// обычным беззнаковым вычитанием, отдельно не обрабатывается).
void computeLocalTime(int &year, int &month, int &day, int &hour, int &minute, int &weekday) {
unsigned long elapsedS = (unsigned long)(millis() - millisAtSync) / 1000UL;
unsigned long nowEpoch = epochAtSync + elapsedS;
long days = (long)(nowEpoch / 86400UL);
unsigned long rem = nowEpoch % 86400UL;
hour = (int)(rem / 3600UL);
minute = (int)((rem % 3600UL) / 60UL);
// epoch 0 (01.01.1970) - четверг; в схеме Пн=0..Вс=6 это индекс 3.
weekday = (int)(((days % 7) + 3 + 7) % 7);
civilFromDays(days, year, month, day);
}
const char *OFFLINE_WEEKDAY_NAMES[7] = {"Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"};
// Общая функция вместо повторяющихся макро-разворачиваний constrain() -
// см. её использование в processCommandLine() при разборе OFFCFG:.
uint16_t clampU16(unsigned long v, unsigned long lo, unsigned long hi) {
if (v < lo) v = lo;
if (v > hi) v = hi;
return (uint16_t)v;
}
// Клампит в диапазон 0-255 и приводит к uint8_t за один вызов - вместо трёх
// однотипных строк "(uint8_t)clampU16(v, 0UL, 255UL)" на каждый r/g/b поля
// OFFCFG: ниже (экономия флеша - см. обсуждение в чате про его дефицит).
uint8_t clampByte(unsigned long v) {
return (uint8_t)clampU16(v, 0UL, 255UL);
}
// Разбирает ОДНО числовое поле из разделённой запятыми строки (см. OFFCFG:
// ниже) - пропускает ведущую запятую, если она есть, и продвигает p за собой
// (strtoul сам сдвигает указатель, см. её сигнатуру). Вынесено в отдельную
// функцию вместо инлайна на КАЖДОЕ поле - строка OFFCFG: теперь несёт 9
// полей (было 4), инлайн-паттерн "if (*p==',') p++; strtoul(...)" повторялся
// бы 8 раз подряд и раздувал бы флеш заметно больше, чем один вызов функции
// на каждое поле.
unsigned long parseNextField(char *&p) {
if (*p == ',') p++;
return strtoul(p, &p, 10);
}
// ---------------- offline-режим: состояние показа ----------------
bool offlineScreenActive = false;       // сейчас показывается офлайн-экран
// (а не просто погашенный watchdog'ом OLED)
unsigned long lastOfflineScreenUpdateMs = 0;
#define OFFLINE_SCREEN_UPDATE_INTERVAL_MS 1000UL   // часы тикают по минутам -
// секундная частота
// перерисовки с большим
// запасом, дороже незачем
// ---- "Ночник": часы по кнопке (см. OfflineConfig.button_clock_s выше) ----
// buttonClockUntilMs - millis()-дедлайн показа офлайн-часов по клику кнопки;
// 0 = сейчас НЕ активен (сентинел вместо отдельного bool-флага - экономит
// байты флеша: одна проверка "!= 0" вместо флага + его синхронизации в
// нескольких местах). Пока != 0, показ НЕ зависит от enabled
// (см. renderOfflineScreenNow()/updateOfflineScreen() ниже) - главное отличие
// от обычного офлайн-экрана.
unsigned long buttonClockUntilMs = 0;
// ---- "Ночник": подсветка ленты сплошным цветом (см. OfflineConfig.led_*) ----
// nightlightRemainingMs - сколько ещё МИЛЛИСЕКУНД должна гореть лента;
// 0 = ночник выключен. Прибавляется/убавляется по NIGHTLIGHT_STEP_MS за
// каждый "клик" энкодера, пока hostTimedOut (см. flushEncoder() ниже) -
// вплоть до NIGHTLIGHT_MAX_MS (защита от "забыл и лента горела всю ночь").
// lastNightlightTickMs - millis() прошлого вызова updateNightlight(), для
// вычитания РЕАЛЬНО прошедшего времени (не "теоретического" тика) -
// обнуляется в момент входа в hostTimedOut (см. checkHostTimeout()), чтобы
// не вычесть время, накопившееся, пока лента вообще была не при делах.
#define NIGHTLIGHT_STEP_MS (60UL * 1000UL)          // 1 минута за "клик" энкодера
#define NIGHTLIGHT_MAX_MS  (60UL * 60UL * 1000UL)   // потолок - 60 минут за раз
long nightlightRemainingMs = 0;
unsigned long lastNightlightTickMs = 0;
bool nightlightWasOn = false;   // был ли ночник УЖЕ зажжён на прошлой итерации -
// чтобы погасить ленту (FastLED.clear()+show())
// РОВНО ОДИН РАЗ при обнулении таймера, а не
// слать show() впустую на каждой последующей
// итерации, пока лента и так уже потушена.
// Рендерит один шаблон офлайн-экрана в out (bounded, null-terminated).
// Поддерживаются ТОЛЬКО пять токенов {time_now}/{weekday_name}/{date_now}/
// {year_now}/{uptime} - без спецификаторов ширины ({var:N}), в отличие от
// обычных L1-3 (те уже полностью отрендерены хостом). Нераспознанный/
// незакрытый токен -> ничего не подставляется (та же логика, что и у
// templates.render() на хосте для нерезолвящейся переменной - пустая
// строка, а не мусор/авария).
void renderOfflineLine(const char *tpl, char *out, size_t outSize, OfflineToken *tokens, uint8_t tokenCount) {
size_t oi = 0;
const char *p = tpl;
while (*p && oi + 1 < outSize) {
if (*p == '{') {
const char *close = strchr(p, '}');
if (close != NULL) {
size_t tokLen = (size_t)(close - (p + 1));
bool matched = false;
for (uint8_t i = 0; i < tokenCount; i++) {
size_t nameLen = strlen(tokens[i].name);
if (nameLen == tokLen && strncmp(p + 1, tokens[i].name, tokLen) == 0) {
const char *v = tokens[i].value;
while (*v && oi + 1 < outSize) out[oi++] = *v++;
matched = true;
break;
}
}
p = close + 1;
if (matched) continue;
continue;  // нераспознанный токен - просто пропускаем {...}
}
}
out[oi++] = *p++;
}
out[oi] = '\0';
}
// Ручное форматирование чисел с ведущими нулями - вместо snprintf("%02d"/
// "%04d",...): на AVR даже целочисленный printf тянет за собой не самую
// маленькую библиотеку разбора формата, а тут нужно только "два/четыре
// знака с нулями слева".
void fmt2(char *out, int v) {
if (v < 0) v = 0;
if (v > 99) v = 99;
out[0] = '0' + (v / 10);
out[1] = '0' + (v % 10);
out[2] = '\0';
}
void fmt4(char *out, int v) {
if (v < 0) v = 0;
if (v > 9999) v = 9999;
out[0] = '0' + (v / 1000);
out[1] = '0' + ((v / 100) % 10);
out[2] = '0' + ((v / 10) % 10);
out[3] = '0' + (v % 10);
out[4] = '\0';
}
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
// Раскладывает N6 hex-символов из value в leds[] через LED_MAP. Если value
// короче/длиннее NUM_LEDS6 - берём min(), лишнее/недостающее игнорируем
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
strncpy(oledLines[idx], value, OLED_LINE_MAX_LEN - 1);
oledLines[idx][OLED_LINE_MAX_LEN - 1] = '\0';
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
   } else if (strcmp(key, "TSYNC") == 0) {
     // unix epoch секунд - умещается в unsigned long (32 бита) до 2106г.
     epochAtSync = strtoul(value, NULL, 10);
     millisAtSync = millis();
     clockSynced = true;
   } else if (strcmp(key, "UPT") == 0) {
     strncpy(lastUptimeText, value, OFFLINE_UPTIME_MAX - 1);
     lastUptimeText[OFFLINE_UPTIME_MAX - 1] = '\0';
   } else if (strcmp(key, "OFFCFG") == 0) {
     // "<enabled>,<timeout_s>,<start_min>,<end_min>,<button_clock_s>,
     //  <led_enabled>,<led_r>,<led_g>,<led_b>" - start_min/end_min (бывшее
     // окно активности) больше НЕ используются: поля остаются в протоколе
     // ради совместимости с хостом, здесь только пропускаются.
     char *p = (char *)value;
     unsigned long en = strtoul(p, &p, 10);
     unsigned long to = parseNextField(p);
     parseNextField(p);   // start_min - игнорируется
     parseNextField(p);   // end_min - игнорируется
     unsigned long bc = parseNextField(p);
     unsigned long le = parseNextField(p);
     unsigned long lr = parseNextField(p);
     unsigned long lg = parseNextField(p);
     unsigned long lb = parseNextField(p);
     offlineConfig.enabled = en ? 1 : 0;
     offlineConfig.timeout_s = clampU16(to, 1UL, 65535UL);
     offlineConfig.button_clock_s = clampU16(bc, 5UL, 300UL);
     offlineConfig.led_enabled = le ? 1 : 0;
     offlineConfig.led_r = clampByte(lr);
     offlineConfig.led_g = clampByte(lg);
     offlineConfig.led_b = clampByte(lb);
   } else if (strcmp(key, "OFFL1") == 0) {
     strncpy(offlineConfig.l1, value, OFFLINE_LINE_MAX - 1);
     offlineConfig.l1[OFFLINE_LINE_MAX - 1] = '\0';
   } else if (strcmp(key, "OFFL2") == 0) {
     strncpy(offlineConfig.l2, value, OFFLINE_LINE_MAX - 1);
     offlineConfig.l2[OFFLINE_LINE_MAX - 1] = '\0';
   } else if (strcmp(key, "OFFL3") == 0) {
     strncpy(offlineConfig.l3, value, OFFLINE_LINE_MAX - 1);
     offlineConfig.l3[OFFLINE_LINE_MAX - 1] = '\0';
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
void renderOfflineScreenNow();  // объявление вперёд - используется ниже
void checkHostTimeout() {
if (hostTimedOut) return;
if (millis() - lastHostDataMs < effectiveTimeoutMs()) return;
hostTimedOut = true;
// "Ночник" - см. докстринг OfflineConfig/globals выше: входим в режим
// "хост пропал" - подсветка ленты и часы-по-кнопке стартуют с чистого
// состояния (0/выключено), а не с того, что могло накопиться раньше
// (например от предыдущего короткого пропадания связи).
nightlightRemainingMs = 0;
lastNightlightTickMs = millis();
buttonClockUntilMs = 0;
// Офлайн-экран показываем, только если: включён в /offline, часы хоть
// раз синхронизировались (TSYNC уже приходил - без него "сейчас" попросту
// неизвестно). Иначе - старое поведение без изменений: просто гасим
// ленту и OLED.
bool wantOffline = offlineConfig.enabled && clockSynced;
FastLED.clear();
FastLED.show();
if (wantOffline) {
offlineScreenActive = true;
lastOfflineScreenUpdateMs = 0;  // форсировать немедленную первую отрисовку
renderOfflineScreenNow();
} else {
offlineScreenActive = false;
oledLines[0][0] = '\0';
oledLines[1][0] = '\0';
oledLines[2][0] = '\0';
oledDirty = true;
}
}
// Пересчитывает и (если что-то изменилось) перерисовывает офлайн-экран -
// вызывается сразу при активации (см. checkHostTimeout()) и дальше раз в
// OFFLINE_SCREEN_UPDATE_INTERVAL_MS, пока offlineScreenActive (см. loop()).
void renderOfflineScreenNow() {
int y, mo, d, h, mi, wd;
computeLocalTime(y, mo, d, h, mi, wd);
char timeBuf[6];       // "ЧЧ:ММ"
char dateBuf[6];       // "ДД.ММ"
char yearBuf[5];       // "ГГГГ"
char hBuf[3], miBuf[3], dBuf[3], moBuf[3];
fmt2(hBuf, h); fmt2(miBuf, mi);
timeBuf[0] = hBuf[0]; timeBuf[1] = hBuf[1]; timeBuf[2] = ':';
timeBuf[3] = miBuf[0]; timeBuf[4] = miBuf[1]; timeBuf[5] = '\0';
fmt2(dBuf, d); fmt2(moBuf, mo);
dateBuf[0] = dBuf[0]; dateBuf[1] = dBuf[1]; dateBuf[2] = '.';
dateBuf[3] = moBuf[0]; dateBuf[4] = moBuf[1]; dateBuf[5] = '\0';
fmt4(yearBuf, y);
const char *weekdayStr = OFFLINE_WEEKDAY_NAMES[wd];
// Ночник: {nightled_status} = Вкл/Выкл, {nightled_time} = оставшееся время
// ММ:СС (секунды округляются вверх, чтобы не показывать 00:00 при ещё
// горящей ленте). Когда ночник выключен - "Выкл" и "00:00".
bool nlOn = offlineConfig.led_enabled && nightlightRemainingMs > 0;
unsigned long nlSecs = nlOn ? (unsigned long)((nightlightRemainingMs + 999L) / 1000L) : 0UL;
char nlMin[3], nlSec[3], nlTime[6];
fmt2(nlMin, (int)(nlSecs / 60UL)); fmt2(nlSec, (int)(nlSecs % 60UL));
nlTime[0] = nlMin[0]; nlTime[1] = nlMin[1]; nlTime[2] = ':';
nlTime[3] = nlSec[0]; nlTime[4] = nlSec[1]; nlTime[5] = '\0';
OfflineToken tokens[] = {
{"nightled_status", nlOn ? "Вкл" : "Выкл"},
{"nightled_time", nlTime},
{"time_now", timeBuf},
{"weekday_name", weekdayStr},
{"date_now", dateBuf},
{"year_now", yearBuf},
{"uptime", lastUptimeText},
};
uint8_t tokenCount = sizeof(tokens) / sizeof(tokens[0]);
char out1[40], out2[40], out3[40];
renderOfflineLine(offlineConfig.l1, out1, sizeof(out1), tokens, tokenCount);
renderOfflineLine(offlineConfig.l2, out2, sizeof(out2), tokens, tokenCount);
renderOfflineLine(offlineConfig.l3, out3, sizeof(out3), tokens, tokenCount);
// Перерисовываем (и сбрасываем скролл), только если текст реально
// изменился - иначе на статичных строках (например без {uptime}) OLED
// бы дёргался лишний раз каждую секунду без всякой пользы.
if (strcmp(oledLines[0], out1) != 0) {
  strncpy(oledLines[0], out1, OLED_LINE_MAX_LEN - 1);
  oledLines[0][OLED_LINE_MAX_LEN - 1] = '\0';
  scrollOffset[0] = 0; oledDirty = true;
}
if (strcmp(oledLines[1], out2) != 0) {
  strncpy(oledLines[1], out2, OLED_LINE_MAX_LEN - 1);
  oledLines[1][OLED_LINE_MAX_LEN - 1] = '\0';
  scrollOffset[1] = 0; oledDirty = true;
}
if (strcmp(oledLines[2], out3) != 0) {
  strncpy(oledLines[2], out3, OLED_LINE_MAX_LEN - 1);
  oledLines[2][OLED_LINE_MAX_LEN - 1] = '\0';
  scrollOffset[2] = 0; oledDirty = true;
}
}
// Вызывается КАЖДУЮ итерацию loop(), пока offlineScreenActive - сама решает,
// не рано ли ещё пересчитывать (см. OFFLINE_SCREEN_UPDATE_INTERVAL_MS).
// Проверка истечения buttonClockUntilMs - ПЕРЕД троттлингом ниже (не раз в
// секунду, а каждую итерацию) - часы-по-кнопке должны погаснуть точно по
// таймеру, не с задержкой до секунды.
void updateOfflineScreen() {
if (!offlineScreenActive) return;
if (buttonClockUntilMs != 0 && millis() >= buttonClockUntilMs) {
buttonClockUntilMs = 0;
// Проверяем, не должен ли офлайн-экран остаться на экране и БЕЗ кнопки -
// по обычному условию (enabled && clockSynced) - если да, просто
// продолжаем показывать его как раньше (ветка ниже это сделает сама).
// Если нет - гасим полностью, экран был показан ИСКЛЮЧИТЕЛЬНО по клику.
bool regularActive = offlineConfig.enabled && clockSynced;
if (!regularActive) {
  offlineScreenActive = false;
  oledLines[0][0] = '\0';
  oledLines[1][0] = '\0';
  oledLines[2][0] = '\0';
  oledDirty = true;
  return;
}
}
unsigned long now = millis();
if (lastOfflineScreenUpdateMs != 0 && now - lastOfflineScreenUpdateMs < OFFLINE_SCREEN_UPDATE_INTERVAL_MS) return;
lastOfflineScreenUpdateMs = now;
renderOfflineScreenNow();
}
// Показывает офлайн-экран на button_clock_s секунд НЕЗАВИСИМО от enabled -
// общий код для клика кнопки (pollButton) и вращения энкодера (flushEncoder:
// чтобы сразу видеть статус/остаток ночника). Без TSYNC: время неизвестно -
// ничего не делает. lastOfflineScreenUpdateMs = 0 - немедленная перерисовка.
void showOfflineClockNow() {
if (!clockSynced) return;
buttonClockUntilMs = millis() + (unsigned long)offlineConfig.button_clock_s * 1000UL;
offlineScreenActive = true;
lastOfflineScreenUpdateMs = 0;
}
// ---- "Ночник" ленты: сплошной цвет, пока хост пропал (см. OfflineConfig.led_*
// и globals nightlightRemainingMs/lastNightlightTickMs/nightlightWasOn выше) ----
// Красим ЦИКЛОМ (leds[i] = CRGB(...)), а НЕ через fill_solid() - тот же
// паттерн присваивания, что уже используется в applyBarValue() выше (там же
// компилятор генерирует код для CRGB(r,g,b) и присваивания в leds[]) -
// fill_solid() из FastLED был бы ПЕРВЫМ использованием этого шаблона в
// прошивке и утянул бы отдельный кусок кода, тогда как ручной цикл
// переиспользует уже скомпилированный путь - экономия флеша (см. обсуждение
// в чате про его дефицит).
void nightlightOff() {
FastLED.clear();
FastLED.show();
nightlightWasOn = false;
}
// Вызывается КАЖДУЮ итерацию loop() (как и updateOfflineScreen()) - сама
// решает, есть ли что делать. Пока nightlightRemainingMs > 0 И led_enabled -
// лента горит offlineConfig.led_r/g/b; иначе гаснет РОВНО ОДИН РАЗ
// (nightlightWasOn), а не на каждой итерации впустую. Момент, когда таймер
// ИМЕННО ЭТИМ тиком дошёл до нуля, отдельно не обрабатывается (в отличие от
// первой версии) - следующая же итерация погасит ленту сама, ценой одного
// невидимого кадра (~40мс при TICK_INTERVAL по умолчанию) - экономит
// дублирующийся код ради визуально неразличимой разницы.
void updateNightlight() {
if (!hostTimedOut) return;
unsigned long now = millis();
unsigned long dt = now - lastNightlightTickMs;
lastNightlightTickMs = now;
if (!offlineConfig.led_enabled || nightlightRemainingMs <= 0) {
nightlightRemainingMs = 0;
if (nightlightWasOn) nightlightOff();
return;
}
nightlightRemainingMs -= (long)dt;
if (nightlightRemainingMs < 0) nightlightRemainingMs = 0;
CRGB c(offlineConfig.led_r, offlineConfig.led_g, offlineConfig.led_b);
for (uint16_t i = 0; i < NUM_LEDS; i++) leds[i] = c;
FastLED.show();
nightlightWasOn = true;
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
// "Ночник": клик показывает офлайн-часы на offlineConfig.button_clock_s
// секунд, НЕЗАВИСИМО от enabled обычного офлайн-экрана
// (см. buttonClockUntilMs в renderOfflineScreenNow()/updateOfflineScreen()
// выше). Требует clockSynced - без хотя бы одного TSYNC: "сейчас" на
// плате попросту неизвестно, показать нечего - клик молча игнорируется,
// как и раньше (см. прежний комментарий про пустую ветку).
showOfflineClockNow();
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
if (delta == 0) return;
if (hostTimedOut) {
// Хост не читает serial - ENC: слать некому (та же причина, что и у
// BTN:CLICK в pollButton() выше). Вместо этого вращение крутит "ночник"
// ленты: по часовой (delta>0) - плюс NIGHTLIGHT_STEP_MS за "клик"
// энкодера, против часовой (delta<0) - минус, до полного выключения
// (нижняя граница 0) и до потолка NIGHTLIGHT_MAX_MS (верхняя) - см.
// updateNightlight() выше за тем, как этот таймер расходуется.
long deltaMs = (long)delta * (long)NIGHTLIGHT_STEP_MS;
nightlightRemainingMs += deltaMs;
if (nightlightRemainingMs < 0) nightlightRemainingMs = 0;
if (nightlightRemainingMs > (long)NIGHTLIGHT_MAX_MS) nightlightRemainingMs = (long)NIGHTLIGHT_MAX_MS;
showOfflineClockNow();   // сразу показать на OLED, сколько минут выставлено
return;
}
Serial.print(F("ENC:"));
if (delta > 0) Serial.print('+');
Serial.println(delta);
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
uint8_t cellW = OLED_CHAR_WIDTH;
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
uint8_t cellW = OLED_CHAR_WIDTH;
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
u8g2.setFont(OLED_FONT_SELECTED);
const int16_t yPos[3] = {OLED_LINE_Y0, OLED_LINE_Y1, OLED_LINE_Y2};
for (uint8_t i = 0; i < 3; i++) {
if (oledLines[i][0] == '\0') continue;
int16_t textWidth = lineWidthPx(oledLines[i]);
if (textWidth <= OLED_WIDTH_PX) {
// короткая строка - печатаем статично, без скролла
drawLineBars(0, yPos[i], oledLines[i]);
} else {
// длинная строка - скроллим влево, зацикливая через OLED_SCROLL_GAP_PX
int16_t x = -scrollOffset[i];
drawLineBars(x, yPos[i], oledLines[i]);
drawLineBars(x + textWidth + OLED_SCROLL_GAP_PX, yPos[i], oledLines[i]);
}
}
} while (u8g2.nextPage());
}
void updateScroll() {
unsigned long now = millis();
if (now - lastScrollMs < OLED_SCROLL_INTERVAL_MS) return;
lastScrollMs = now;
for (uint8_t i = 0; i < 3; i++) {
if (oledLines[i][0] == '\0') continue;
int16_t textWidth = lineWidthPx(oledLines[i]);
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
// Хост прислал хоть что-то - считаем его живым и сбрасываем watchdog
// ЗДЕСЬ, а не внутри checkHostTimeout()/processCommandLine() - это
// единственное место, где действительно известно "от хоста только что
// пришла целая строка" (CAL из Serial Monitor тоже считается - это
// тоже говорит о том, что порт живой и с той стороны кто-то есть).
lastHostDataMs = millis();
hostTimedOut = false;
offlineScreenActive = false;  // хост вернулся - офлайн-экран больше не
// актуален, дальше oledLines[] перезапишут
// обычные L1-3: из processCommandLine() ниже
// "Ночник" - хост вернулся, оба его механизма больше не актуальны:
// часы-по-кнопке гасим (см. offlineScreenActive выше), таймер
// подсветки ленты обнуляем и, если она горела, тушим - хотя leds[]
// и так будет тут же перезаписан первым же BAR: внутри
// processCommandLine() ниже, лишний clear() дешёвый и не полагается
// на порядок полей в строке от хоста.
buttonClockUntilMs = 0;
nightlightRemainingMs = 0;
if (nightlightWasOn) nightlightOff();
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
updateOfflineScreen();
updateNightlight();
updateScroll();
if (oledDirty) {
drawOled();
oledDirty = false;
}
}