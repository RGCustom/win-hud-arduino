"""
metrics_windows.py  (win-hud-arduino)

Сбор метрик Windows-хоста - аналог блока чтения /proc/* в shkaf-hud
(shkaf_stats_bridge.py), но через штатные Windows API вместо procfs:

    CPU/RAM/диски/сеть  -> psutil
    GPU (NVIDIA)        -> pynvml (nvidia-ml-py)
    Громкость/mute/устройство вывода -> pycaw (обёртка над Core Audio API)
    Раскладка клавиатуры -> ctypes (user32.dll)

ВАЖНО про звук: реализация ниже писалась и проверялась логически (не
исполнялась - у меня нет доступа к настоящей Windows-машине с живым Core
Audio API), т.к. pycaw/comtypes используют Windows-only COM-интерфейсы.
Структура функций и именование - стандартный для pycaw паттерн, но при
первом запуске на реальном железе (Konstantin) стоит внимательно проверить
блок AudioController, особенно чтение FriendlyName устройства - там больше
всего шансов, что понадобится мелкая правка под конкретную версию pycaw.

Зависимости (requirements.txt):
    psutil, pynvml, pycaw, comtypes, pywin32

Ничего не знает про Flask/serial/протокол - чистый сбор данных, вызывается
из pc_hud.py раз в тик (и отдельно - из обработчика ENC/BTN событий для
audio-функций).
"""

import ctypes
import time

import psutil

try:
    import pynvml
    _NVML_AVAILABLE = True
except ImportError:
    _NVML_AVAILABLE = False

try:
    import comtypes
    from comtypes import CLSCTX_ALL, COMError
    from ctypes import POINTER, cast
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    _PYCAW_AVAILABLE = True
except ImportError:
    _PYCAW_AVAILABLE = False


# ---------------- CPU / RAM ----------------

def read_cpu_stats():
    """
    (cpu_pct, cpu_pct_core_max, cpu_freq_mhz).

    psutil.cpu_percent(percpu=True) сам ведёт внутренний стейт между вызовами
    (сравнивает с предыдущим вызовом) - в отличие от shkaf-hud, тут не нужно
    вручную носить prev_idle/prev_total между тиками главного цикла.
    Первый вызов после старта процесса вернёт 0.0 по всем ядрам - это
    штатное поведение psutil, второй тик уже даст осмысленные цифры.
    """
    per_core = psutil.cpu_percent(percpu=True)
    cpu_pct = sum(per_core) / len(per_core) if per_core else 0.0
    cpu_pct_core_max = max(per_core) if per_core else 0.0

    freq = psutil.cpu_freq()
    cpu_freq_mhz = round(freq.current) if freq else None

    return cpu_pct, cpu_pct_core_max, cpu_freq_mhz


def read_ram_stats():
    """(ram_pct, ram_used_gb, ram_total_gb)."""
    vm = psutil.virtual_memory()
    used_gb = round((vm.total - vm.available) / (1024 ** 3), 1)
    total_gb = round(vm.total / (1024 ** 3), 1)
    return vm.percent, used_gb, total_gb


# ---------------- GPU (NVIDIA, через pynvml) ----------------

class GpuMonitor:
    """
    Обёртка над pynvml - держит nvmlInit()/handle живыми между тиками
    (открывать заново на каждый опрос дорого и не нужно). Если pynvml не
    установлен или NVIDIA-карта не найдена - все методы тихо возвращают
    "пустые" значения (0/None), чтобы главный цикл не падал на PC без GPU
    или при временной проблеме с драйвером.
    """

    def __init__(self, device_index=0):
        self.available = False
        self.handle = None
        self.name = None
        if not _NVML_AVAILABLE:
            print("[gpu] pynvml не установлен - GPU-метрики недоступны", flush=True)
            return
        try:
            pynvml.nvmlInit()
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(device_index)
            raw_name = pynvml.nvmlDeviceGetName(self.handle)
            # старые версии pynvml отдают bytes, новые - str
            self.name = raw_name.decode() if isinstance(raw_name, bytes) else raw_name
            self.available = True
        except Exception as e:
            print(f"[gpu] nvmlInit/GetHandle failed: {e}", flush=True)

    def read(self):
        """
        Возвращает dict: gpu_pct, gpu_temp_c, gpu_vram_used_gb, gpu_vram_total_gb,
        gpu_vram_pct, gpu_power_w, gpu_name. Поля, которые не удалось прочитать
        (например power на картах без поддержки) - None, а не 0, чтобы экраны
        могли отличить "нет данных" от "буквально ноль".
        """
        empty = {
            "gpu_name": self.name or "N/A",
            "gpu_pct": 0.0, "gpu_temp_c": None,
            "gpu_vram_used_gb": 0.0, "gpu_vram_total_gb": 0.0, "gpu_vram_pct": 0.0,
            "gpu_power_w": None,
        }
        if not self.available:
            return empty

        try:
            util = pynvml.nvmlDeviceGetUtilizationRates(self.handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(self.handle)

            try:
                temp_c = pynvml.nvmlDeviceGetTemperature(self.handle, pynvml.NVML_TEMPERATURE_GPU)
            except Exception:
                temp_c = None

            try:
                power_w = round(pynvml.nvmlDeviceGetPowerUsage(self.handle) / 1000.0, 1)
            except Exception:
                power_w = None

            vram_used_gb = round(mem.used / (1024 ** 3), 2)
            vram_total_gb = round(mem.total / (1024 ** 3), 2)
            vram_pct = round(mem.used / mem.total * 100.0) if mem.total else 0.0

            return {
                "gpu_name": self.name or "N/A",
                "gpu_pct": float(util.gpu),
                "gpu_temp_c": temp_c,
                "gpu_vram_used_gb": vram_used_gb,
                "gpu_vram_total_gb": vram_total_gb,
                "gpu_vram_pct": vram_pct,
                "gpu_power_w": power_w,
            }
        except Exception as e:
            print(f"[gpu] read failed: {e}", flush=True)
            return empty

    def shutdown(self):
        if self.available:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass


# ---------------- Диски ----------------

def list_disk_letters():
    """Список букв дисков вида ['C', 'D', ...] - для выпадающих списков
    disk1_letter/disk2_letter в /settings (аналог list_real_interfaces() в
    shkaf-hud). Пропускает CD-приводы и недоступные тома (psutil.disk_usage
    может кинуть исключение на пустом приводе)."""
    letters = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        drive = part.mountpoint.rstrip("\\/")  # 'C:\\' -> 'C:'
        letter = drive.rstrip(":")
        try:
            psutil.disk_usage(part.mountpoint)
        except (OSError, PermissionError):
            continue
        letters.append(letter)
    return letters


def read_disk_usage(letter):
    """(used_pct, free_gb, total_gb) для буквы диска, либо None при ошибке
    (диск отключён/недоступен - например внешний накопитель вынут)."""
    if not letter:
        return None
    try:
        usage = psutil.disk_usage(f"{letter}:\\")
    except (OSError, PermissionError):
        return None
    total_gb = round(usage.total / (1024 ** 3), 1)
    free_gb = round(usage.free / (1024 ** 3), 1)
    used_pct = round(usage.percent)
    return {"used_pct": used_pct, "free_gb": free_gb, "total_gb": total_gb}


# ---------------- Сеть ----------------

def list_network_interfaces():
    """Имена сетевых адаптеров, у которых линк поднят (isup) - для
    net1_iface/net2_iface в веб-интерфейсе (аналог list_real_interfaces())."""
    stats = psutil.net_if_stats()
    return [name for name, s in stats.items() if s.isup]


def read_iface_counters(iface):
    """(rx_bytes, tx_bytes) - накопленные счётчики интерфейса с момента
    загрузки ОС (не с запуска нашего процесса - как read_net_bytes() в
    shkaf-hud, разница считается между двумя тиками в главном цикле)."""
    counters = psutil.net_io_counters(pernic=True)
    entry = counters.get(iface)
    if entry is None:
        return None, None
    return entry.bytes_recv, entry.bytes_sent


def read_iface_speed_mbps(iface):
    """Скорость линка в Mbps (0/None, если адаптер не отдаёт эту информацию -
    обычное дело для виртуальных адаптеров)."""
    stats = psutil.net_if_stats().get(iface)
    return stats.speed if stats else None


# ---------------- Звук (громкость/mute/устройство вывода) ----------------
# Настраиваемое действие на клик энкодера (mute/unmute, переключение
# устройства и т.п.) читается из settings.json главным циклом - здесь только
# сами примитивы, которыми это действие пользуется.

class AudioController:
    """
    Обёртка над pycaw для чтения/управления громкостью ПО УМОЛЧАНИЮ
    выводящего устройства. Один инстанс на процесс - COM-объекты внутри
    кэшируются, т.к. пересоздавать их на каждый тик (раз в секунду + на
    каждое событие энкодера) избыточно дорого.

    ВАЖНО: comtypes требует, чтобы COM был инициализирован (CoInitialize) в
    ТОМ ЖЕ потоке, из которого вызываются его методы. Если AudioController
    используется из отдельного потока (например из обработчика serial-
    событий энкодера, который может жить в своём потоке в pc_hud.py) -
    нужно явно вызвать comtypes.CoInitialize() в начале этого потока один
    раз, иначе будет падать с COMError при первом обращении.
    """

    def __init__(self):
        self.available = _PYCAW_AVAILABLE
        self._volume_iface = None
        if not self.available:
            print("[audio] pycaw не установлен - управление громкостью недоступно", flush=True)

    def _get_volume_interface(self):
        """Ленивая инициализация + автопересоздание, если устройство вывода
        сменилось (девайс мог быть переключён вручную в Windows между
        тиками - тогда старый COM-указатель может быть уже невалиден)."""
        try:
            device = AudioUtilities.GetSpeakers()
            iface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            self._volume_iface = cast(iface, POINTER(IAudioEndpointVolume))
            return self._volume_iface
        except COMError as e:
            print(f"[audio] GetSpeakers/Activate failed: {e}", flush=True)
            return None

    def read_state(self):
        """dict: volume_pct (0-100 int), volume_muted ('да'/'нет'),
        audio_device_name (str). Возвращает нули/N-A при недоступности."""
        empty = {"volume_pct": 0, "volume_muted": "?", "audio_device_name": "N/A"}
        if not self.available:
            return empty

        vol = self._get_volume_interface()
        if vol is None:
            return empty

        try:
            level = vol.GetMasterVolumeLevelScalar()  # 0.0 - 1.0
            muted = bool(vol.GetMute())
            device_name = self._read_device_name()
            return {
                "volume_pct": round(level * 100),
                "volume_muted": "да" if muted else "нет",
                "audio_device_name": device_name,
            }
        except COMError as e:
            print(f"[audio] read_state failed: {e}", flush=True)
            return empty

    def _read_device_name(self):
        """FriendlyName текущего устройства вывода по умолчанию. pycaw не
        даёт это напрямую через IAudioEndpointVolume - идём через
        AudioUtilities.GetAllDevices() и ищем активное устройство вывода по
        умолчанию. Это самое хрупкое место модуля - см. предупреждение в
        шапке файла."""
        try:
            for d in AudioUtilities.GetAllDevices():
                if getattr(d, "id", None) and d.state == 1:  # DEVICE_STATE.ACTIVE
                    return d.FriendlyName
        except Exception as e:
            print(f"[audio] device name lookup failed: {e}", flush=True)
        return "N/A"

    def set_volume_relative(self, steps):
        """Изменить громкость на steps процентных пунктов (положительное =
        громче, отрицательное = тише). Размер шага на один "тик" энкодера -
        настройка в settings.json (volume_step_pct), считается в pc_hud.py,
        сюда приходит уже готовое число."""
        if not self.available:
            return
        vol = self._get_volume_interface()
        if vol is None:
            return
        try:
            current = vol.GetMasterVolumeLevelScalar()
            new_level = max(0.0, min(1.0, current + steps / 100.0))
            vol.SetMasterVolumeLevelScalar(new_level, None)
        except COMError as e:
            print(f"[audio] set_volume_relative failed: {e}", flush=True)

    def toggle_mute(self):
        if not self.available:
            return
        vol = self._get_volume_interface()
        if vol is None:
            return
        try:
            vol.SetMute(0 if vol.GetMute() else 1, None)
        except COMError as e:
            print(f"[audio] toggle_mute failed: {e}", flush=True)

    def switch_output_device(self):
        """
        TODO: переключение устройства вывода по умолчанию НЕ реализовано в
        этой версии - штатного публичного Windows API для этого нет (нужен
        недокументированный COM-интерфейс IPolicyConfig, либо внешняя
        утилита вроде NirSoft SoundVolumeView/AudioDeviceCmdlets). Раз клик
        энкодера ещё не решён окончательно (mute/unmute vs switch device) -
        отложил до момента, когда решим, каким способом это делать надёжно.
        Пока просто no-op с логом, чтобы не падать, если это назначат на клик.
        """
        print("[audio] switch_output_device: пока не реализовано (см. TODO в коде)", flush=True)


# ---------------- Раскладка клавиатуры ----------------

# Primary language ID (младшие 10 бит LANGID) -> короткое имя для OLED.
# Список не претендует на полноту - расширяется по мере необходимости
# (что реально используется на конкретной машине).
_PRIMARY_LANG_NAMES = {
    0x09: "EN",  # English (любой регион - US/UK/etc.)
    0x19: "RU",  # Russian
    0x22: "UA",  # Ukrainian
    0x07: "DE",  # German
    0x0C: "FR",  # French
    0x0A: "ES",  # Spanish
    0x10: "IT",  # Italian
    0x15: "PL",  # Polish
}


def get_keyboard_layout():
    """
    Текущая раскладка клавиатуры - по решению из обсуждения берём "глобальную
    системную" в упрощённом виде: раскладку потока переднего окна (foreground
    window), опрашиваемую поллингом раз в тик, БЕЗ хуков на переключение
    фокуса/языка. Это самый простой надёжный способ без win32-message-hook
    инфраструктуры, и в обычном режиме (без индивидуальной раскладки на
    приложение, включаемой отдельной опцией Windows) он показывает ровно то
    же значение, что и системный индикатор языка в трее.

    Возвращает короткое имя ('EN'/'RU'/...) или '??' если язык не опознан
    по таблице выше, или None при ошибке доступа к API.
    """
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        thread_id = user32.GetWindowThreadProcessId(hwnd, None)
        hkl = user32.GetKeyboardLayout(thread_id)
        lang_id = hkl & 0xFFFF
        primary_lang = lang_id & 0x3FF
        return _PRIMARY_LANG_NAMES.get(primary_lang, "??")
    except Exception:
        return None


# ---------------- самотест модуля (запуск напрямую: python metrics_windows.py) ----------------

if __name__ == "__main__":
    print("CPU:", read_cpu_stats())
    print("RAM:", read_ram_stats())
    print("Disks:", list_disk_letters())
    print("Net ifaces:", list_network_interfaces())
    print("Keyboard layout:", get_keyboard_layout())

    gpu = GpuMonitor()
    print("GPU:", gpu.read())
    gpu.shutdown()

    audio = AudioController()
    print("Audio:", audio.read_state())
