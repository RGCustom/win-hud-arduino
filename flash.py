"""
flash.py  (win-hud-arduino)

Удалённая прошивка Arduino Pro Micro (Leonardo-совместимая, ATmega32u4)
через avrdude - портировано из shkaf-hud. Механика 1200bps-touch и логика
поиска bootloader-порта (два сценария: "появился новый порт" / "старый порт
пропал и вернулся") НЕ ИЗМЕНИЛИСЬ - они не завязаны на конкретную ОС, только
на поведение самого Leonardo-бутлоадера. Изменился только источник списка
портов: `serial.tools.list_ports` (кроссплатформенный API pyserial) вместо
glob по /dev/ttyACM* - на Windows порты называются COM3/COM7/... и не имеют
файлового пути в файловой системе, который можно было бы glob'ить.

Также убран шаг "резолвить by-id symlink" (в shkaf-hud - resolve /dev/serial/
by-id/... в /dev/ttyACMx перед touch, чтобы не потерять устройство, когда
симлинк на секунды пропадёт вместе с платой при сбросе) - на Windows такого
стабильного by-id-пути в принципе нет, серийный порт настраивается в
/settings просто именем COM-порта (например "COM5"), которое и используется
напрямую как original_device.

Требует avrdude в PATH (на Windows - например из установки Arduino IDE,
которая кладёт avrdude.exe в свою папку hardware/tools/avr/bin - эту папку
нужно добавить в PATH, либо указать полный путь через AVRDUDE_PATH в
settings.json/переменной окружения - см. TODO ниже, если понадобится).

Ничего не знает про Flask/HTTP - чистая логика, вызывается из flash_webui.py.
"""

import os
import queue
import subprocess
import threading
import time

import serial
import serial.tools.list_ports

BAUD_TOUCH = 1200
AVRDUDE_BAUD = 57600
BOOTLOADER_WAIT_TIMEOUT = 5.0
BOOTLOADER_POLL_INTERVAL = 0.2
BOOTLOADER_SETTLE_DELAY = 0.5  # дать бутлоадеру подняться, прежде чем дёргать avrdude
AVRDUDE_TIMEOUT = 60.0  # секунд - типичная заливка занимает 5-15с, с большим запасом

# Windows не всегда кладёт avrdude в PATH сам по себе (в отличие от Docker-
# образа shkaf-hud, где `apt install avrdude` решал это раз и навсегда) -
# если бинарник не найден в PATH, можно переопределить полным путём через
# переменную окружения AVRDUDE_PATH (например путь внутрь установки Arduino IDE).
AVRDUDE_EXE = os.environ.get("AVRDUDE_PATH", "avrdude")


class FlashError(Exception):
    """Любая ошибка на этапах touch/поиск порта/avrdude - с человекочитаемым текстом."""


def list_com_ports():
    """Все последовательные порты прямо сейчас - имена вида 'COM5' (аналог
    list_acm_ports() в shkaf-hud, только через pyserial вместо glob по /dev,
    т.к. на Windows нет файловой системы устройств для glob'а)."""
    return {p.device for p in serial.tools.list_ports.comports()}


def touch_1200bps_reset(port):
    """Открыть-закрыть порт на 1200 бод - штатный способ попросить
    Leonardo-бутлоадер перезагрузиться в режим прошивки. Работает одинаково
    на Windows и Linux - это поведение самой платы, не драйвера ОС."""
    try:
        s = serial.Serial(port, BAUD_TOUCH)
        s.close()
    except (serial.SerialException, OSError) as e:
        raise FlashError(f"не удалось открыть {port} на 1200 бод (touch): {e}")


def wait_for_bootloader_port(before_ports, original_device, timeout=BOOTLOADER_WAIT_TIMEOUT):
    """
    Ждём, пока плата поднимется в режиме бутлоадера. Два возможных сценария
    после touch (нумерация COM-портов после сброса платы не гарантированно
    стабильна, аналогично ttyACM на Linux):

      1. Плата поднимается под НОВЫМ именем (например была COM5, бутлоадер
         поднялся как COM7) - ловим появление порта, которого не было в
         before_ports.
      2. Плата пропадает и появляется снова под ТЕМ ЖЕ именем (COM5 исчез на
         время сброса и снова стал COM5) - тогда "новых" портов не появится
         никогда, нужно отдельно отследить, что original_device пропадал
         из списка, а потом вернулся - это и есть сигнал готовности.

    Возвращает имя bootloader-порта или бросает FlashError по таймауту.
    """
    deadline = time.time() + timeout
    seen_disappear = False

    while time.time() < deadline:
        now_ports = list_com_ports()

        new_ports = now_ports - before_ports
        if new_ports:
            return sorted(new_ports)[0]

        if original_device not in now_ports:
            seen_disappear = True
        elif seen_disappear:
            return original_device

        time.sleep(BOOTLOADER_POLL_INTERVAL)

    raise FlashError(
        "плата не вошла в режим прошивки за отведённое время "
        "(после touch на 1200 бод порт не пропадал/не появлялся заново)"
    )


def flash(hex_path, serial_port, mcu="atmega32u4", cancel_event=None):
    """
    Генератор: делает touch + ищет bootloader-порт + запускает avrdude,
    построчно yield-ит текстовые статус-сообщения (включая живой вывод
    avrdude). Последняя строка - "OK" при успехе; при ошибке бросает
    FlashError (вызывающий код должен поймать и отдать как есть).

    cancel_event - опциональный threading.Event: если выставлен пользователем
    (кнопка "Отмена" в /flash) во время работы avrdude, процесс принудительно
    убивается тем же способом, что и при таймауте, только с другим текстом
    ошибки.
    """
    if not os.path.isfile(hex_path):
        raise FlashError(f"файл прошивки не найден: {hex_path}")
    if os.path.getsize(hex_path) == 0:
        raise FlashError("файл прошивки пустой")

    available_ports = list_com_ports()
    if serial_port not in available_ports:
        raise FlashError(
            f"плата не найдена на {serial_port} - проверь, что Pro Micro "
            f"физически подключена и порт в настройках верный "
            f"(Диспетчер устройств -> Порты (COM и LPT) в Windows)"
        )

    yield f"Ищу плату на {serial_port}..."
    before_ports = available_ports
    # На Windows нет стабильного by-id-пути как на Linux - COM-имя из
    # настроек используется напрямую, резолвить symlink не нужно.
    original_device = serial_port

    yield "Отправляю сигнал перезагрузки в бутлоадер (1200 бод touch)..."
    touch_1200bps_reset(serial_port)

    yield "Жду появления bootloader-порта..."
    bootloader_port = wait_for_bootloader_port(before_ports, original_device)
    yield f"Бутлоадер поднялся на {bootloader_port}, жду {BOOTLOADER_SETTLE_DELAY:.1f}с..."
    time.sleep(BOOTLOADER_SETTLE_DELAY)

    cmd = [
        AVRDUDE_EXE,
        "-c", "avr109",
        "-p", mcu,
        "-P", bootloader_port,
        "-b", str(AVRDUDE_BAUD),
        "-D",
        "-U", f"flash:w:{hex_path}:i",
    ]
    yield f"Запускаю avrdude: {' '.join(cmd)}"

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as e:
        raise FlashError(
            f"не удалось запустить avrdude ({AVRDUDE_EXE}): {e} - "
            f"проверь, что avrdude установлен и доступен в PATH (или укажи "
            f"полный путь через переменную окружения AVRDUDE_PATH)"
        )

    output_q = queue.Queue()

    def _pump_output(pipe, q):
        for line in iter(pipe.readline, ""):
            q.put(line)
        q.put(None)  # сигнал конца потока

    reader_thread = threading.Thread(target=_pump_output, args=(proc.stdout, output_q), daemon=True)
    reader_thread.start()

    start_time = time.time()
    timed_out = False
    cancelled = False

    while True:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        remaining = AVRDUDE_TIMEOUT - (time.time() - start_time)
        if remaining <= 0:
            timed_out = True
            break
        try:
            line = output_q.get(timeout=min(remaining, 0.5))
        except queue.Empty:
            continue
        if line is None:
            break
        yield line.rstrip("\n")

    if cancelled:
        proc.kill()
        proc.wait()
        raise FlashError("прошивка отменена пользователем")

    if timed_out:
        proc.kill()
        proc.wait()
        raise FlashError(
            f"avrdude не уложился в {AVRDUDE_TIMEOUT:.0f}с и был принудительно "
            f"остановлен - похоже, связь с платой оборвалась во время заливки"
        )

    proc.wait()
    if proc.returncode != 0:
        raise FlashError(f"avrdude завершился с кодом {proc.returncode} - см. лог выше")

    yield "OK"
