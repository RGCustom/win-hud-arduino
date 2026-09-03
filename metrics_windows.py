"""
metrics_windows.py  (win-hud-arduino)

Сбор метрик Windows-хоста - аналог блока чтения /proc/* в shkaf-hud
(shkaf_stats_bridge.py), но через штатные Windows API вместо procfs:

    CPU/RAM/диски/сеть  -> psutil
    GPU (NVIDIA)        -> pynvml (nvidia-ml-py)
    Громкость/mute/устройство вывода -> pycaw (обёртка над Core Audio API)
    Раскладка клавиатуры -> ctypes (user32.dll)

ВАЖНО про звук: изначальная реализация писалась без доступа к настоящей
Windows-машине и была скорректирована по фидбеку с реального запуска -
в частности, AudioUtilities.GetSpeakers() в pycaw 2024+ возвращает готовую
обёртку с .EndpointVolume/.FriendlyName напрямую (без ручного Activate()) -
см. комментарий в AudioController._get_volume_interface() ниже. Основная
механика (получение и кэширование интерфейса громкости) сейчас проверена
и работает; switch_output_device() всё ещё не реализован (см. TODO там же).

Зависимости (requirements.txt):
    psutil, pynvml, pycaw, comtypes, pywin32

Ничего не знает про Flask/serial/протокол - чистый сбор данных, вызывается
из pc_hud.py раз в тик (и отдельно - из обработчика ENC/BTN событий для
audio-функций).
"""

import asyncio
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

if _PYCAW_AVAILABLE:
    from comtypes import GUID, IUnknown, COMMETHOD, HRESULT as _HRESULT
    from ctypes import c_float, c_uint32

    class _IAudioMeterInformationFull(IUnknown):
        """
        Полное объявление COM-интерфейса IAudioMeterInformation - см. падение
        "AttributeError: ... object has no attribute 'GetMeteringChannelCount'"
        на реальном запуске: pycaw.api.endpointvolume.IAudioMeterInformation в
        установленной версии pycaw объявляет ТОЛЬКО GetPeakValue (известная
        проблема самого pycaw - см. github.com/AndreMiras/pycaw issue #62).

        COM - это контракт бинарной vtable (таблицы указателей на функции по
        фиксированным смещениям). Реальный объект в памяти Windows поддерживает
        ВСЕ 4 метода независимо от того, что объявлено в Python-обёртке - тут
        просто дополняем недостающие, СТРОГО в порядке их реального положения
        в интерфейсе (endpointvolume.h), иначе вызов уйдёт по чужому смещению
        vtable и либо упадёт, либо (хуже) молча вызовет не тот метод:
            1. GetPeakValue
            2. GetMeteringChannelCount
            3. GetChannelsPeakValues
            4. QueryHardwareSupport   (не используется нами - можно не
                                        объявлять: он последний, поэтому
                                        отсутствие не сдвигает смещения
                                        трёх предыдущих методов)

        GetChannelsPeakValues.afPeakValues объявлен как "in", а НЕ "out" -
        это буфер, который в реальном Win32 API выделяет и передаёт ВЫЗЫВАЮЩИЙ
        (caller-allocated array), а не COM-объект. Если пометить его "out",
        comtypes попытается сам сгенерировать буфер под ОДИН float (как для
        GetPeakValue), и Core Audio запишет за границы этого буфера при
        стерео/многоканальном звуке - именно поэтому buf передаётся вручную
        в read_vu() ниже, а не создаётся автоматически.
        """
        _iid_ = GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
        _methods_ = (
            COMMETHOD([], _HRESULT, "GetPeakValue",
                      (["out"], ctypes.POINTER(c_float), "pfPeak")),
            COMMETHOD([], _HRESULT, "GetMeteringChannelCount",
                      (["out"], ctypes.POINTER(c_uint32), "pnChannelCount")),
            COMMETHOD([], _HRESULT, "GetChannelsPeakValues",
                      (["in"], c_uint32, "u32ChannelCount"),
                      (["in"], ctypes.POINTER(c_float), "afPeakValues")),
        )

try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _MediaManager,
    )
    _WINSDK_AVAILABLE = True
except ImportError:
    _WINSDK_AVAILABLE = False


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

def init_com_for_thread():
    """
    Инициализирует COM (CoInitialize) для ТЕКУЩЕГО потока - обязательный
    вызов перед первым использованием AudioController из любого потока,
    кроме того, где сам процесс стартовал (см. предупреждение в докстринге
    AudioController ниже). В pc_hud.py вызывается один раз в самом начале
    metrics_main_loop().

    Без этого pycaw падает с "OSError: [WinError -2147221008] Не был
    произведен вызов CoInitialize" при первом обращении к звуку из фонового
    потока - именно так и происходит, если main-поток (там, где Python сам
    неявно инициализирует COM STA при старте) не совпадает с потоком,
    реально дёргающим AudioUtilities/IAudioEndpointVolume.

    Безопасно вызывать повторно (comtypes сам игнорирует повторный
    CoInitialize в том же потоке) и безопасно вызывать, даже если pycaw не
    установлен - тогда просто no-op.
    """
    if not _PYCAW_AVAILABLE:
        return
    try:
        comtypes.CoInitialize()
    except OSError:
        # уже инициализирован в этом потоке (RPC_E_CHANGED_MODE и т.п. -
        # для наших целей это не ошибка, COM всё равно доступен)
        pass


class AudioController:
    """
    Обёртка над pycaw для чтения/управления громкостью ПО УМОЛЧАНИЮ
    выводящего устройства. Один инстанс на процесс - COM-объекты внутри
    кэшируются, т.к. пересоздавать их на каждый тик (раз в секунду + на
    каждое событие энкодера) избыточно дорого.

    ВАЖНО: comtypes требует, чтобы COM был инициализирован (CoInitialize) в
    ТОМ ЖЕ потоке, из которого вызываются его методы - см. init_com_for_thread()
    выше. pc_hud.py зовёт её один раз в начале metrics_main_loop(), поэтому
    сам AudioController об этом можно больше не думать при обычном
    использовании - но если будешь дёргать его из ЕЩЁ ОДНОГО потока (кроме
    metrics_main_loop), для того потока тоже нужен свой init_com_for_thread().
    """

    def __init__(self):
        self.available = _PYCAW_AVAILABLE
        self._volume_iface = None
        self._device_name = None
        self._meter_iface = None
        # "Отображаемые" (уже сглаженные затуханием) VU-уровни - состояние
        # между тиками, см. read_vu() ниже. Сразу в процентах (0-100) - так
        # удобнее отдавать напрямую в common_metrics ленты (pc_hud.py).
        self._vu_peak = 0.0
        self._vu_left = 0.0
        self._vu_right = 0.0
        if not self.available:
            print("[audio] pycaw не установлен - управление громкостью недоступно", flush=True)

    def _get_volume_interface(self):
        """Ленивая инициализация + автопересоздание, если устройство вывода
        сменилось (девайс мог быть переключён вручную в Windows между
        тиками - тогда старый COM-указатель может быть уже невалиден).

        pycaw 2024+ (у нас в проекте) возвращает из GetSpeakers() готовую
        обёртку AudioDevice с атрибутами .EndpointVolume и .FriendlyName
        напрямую - никакого ручного Activate()/cast() уже не нужно, и заодно
        отпадает нужда в хрупком переборе AudioUtilities.GetAllDevices() для
        имени устройства (см. _read_device_name() ниже - теперь просто читает
        закэшированное имя). Более старые версии pycaw (до этого изменения
        API) такого атрибута не имеют - для них оставлен путь через
        Activate()+cast(), определяется по hasattr() ниже."""
        try:
            device = AudioUtilities.GetSpeakers()
            if hasattr(device, "EndpointVolume"):
                self._volume_iface = device.EndpointVolume
                self._device_name = getattr(device, "FriendlyName", "N/A")
            else:
                iface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                self._volume_iface = cast(iface, POINTER(IAudioEndpointVolume))
                self._device_name = None  # неизвестно - см. _read_device_name() fallback
            return self._volume_iface
        except COMError as e:
            print(f"[audio] GetSpeakers failed: {e}", flush=True)
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
        """FriendlyName текущего устройства вывода по умолчанию. На новом
        pycaw уже закэширован в _get_volume_interface() (см. выше) - тут
        просто отдаём его. На старом pycaw (без .EndpointVolume) кэш будет
        None - тогда как fallback идём через AudioUtilities.GetAllDevices()."""
        if self._device_name is not None:
            return self._device_name
        try:
            for d in AudioUtilities.GetAllDevices():
                if getattr(d, "id", None) and d.state == 1:  # DEVICE_STATE.ACTIVE
                    return d.FriendlyName
        except Exception as e:
            print(f"[audio] device name lookup failed: {e}", flush=True)
        return "N/A"

    def _get_meter_interface(self):
        """
        Лениво создаёт/пересоздаёт IAudioMeterInformation для текущего
        устройства вывода по умолчанию - источник данных для VU-метра ленты
        (см. read_vu() ниже и vu_peak/vu_left/vu_right в pc_hud.py). Это
        ОТДЕЛЬНЫЙ COM-интерфейс от EndpointVolume (громкость и метринг у
        Core Audio - разные интерфейсы ОДНОГО И ТОГО ЖЕ endpoint-объекта),
        поэтому кэшируется в своём поле, но пересоздаётся по тем же
        правилам, что и volume-интерфейс (устройство вывода могло смениться
        вручную между тиками - см. GetSpeakers() ниже, он всегда возвращает
        АКТУАЛЬНОЕ дефолтное устройство на момент вызова).

        ВАЖНО (проверено чтением исходников pycaw.utils.AudioDevice): у
        обёртки AudioDevice, в отличие от volume, НЕТ готового свойства
        .MeterInformation - только .EndpointVolume. Но Activate() нельзя
        звать на самой обёртке AudioDevice (у неё такого метода нет - это
        и была причина падения "'AudioDevice' object has no attribute
        'Activate'"), только на её приватном атрибуте ._dev (сырой
        IMMDevice COM-объект) - именно так устроено внутри самого pycaw
        свойство .EndpointVolume (см. pycaw/utils.py). Дублируем тот же
        паттерн вручную: device._dev.Activate(...).QueryInterface(...).
        """
        try:
            device = AudioUtilities.GetSpeakers()
            iface = device._dev.Activate(_IAudioMeterInformationFull._iid_, CLSCTX_ALL, None)
            self._meter_iface = iface.QueryInterface(_IAudioMeterInformationFull)
            return self._meter_iface
        except (COMError, OSError, AttributeError) as e:
            print(f"[audio] GetMeterInformation failed: {e}", flush=True)
            return None

    VU_RELEASE_SECONDS = 0.15  # время плавного спада показанного пика -
                                # визуальная характеристика ленты, поэтому
                                # константа тут, а не настройка в /settings
                                # (аналогично OLED_SCROLL_STEP_PX в .ino).
                                # Понижено с прежних 0.3с - для
                                # VU-эквалайзера это ощущалось вязко;
                                # 0.15с ближе к тому, как затухают полосы
                                # в типичных спектр-барах плееров (Winamp/
                                # foobar2000 и т.п.). Не путать с
                                # TICK_INTERVAL в pc_hud.py - это два
                                # РАЗНЫХ рычага "отзывчивости": TICK_INTERVAL
                                # - как часто вообще опрашиваем звук,
                                # VU_RELEASE_SECONDS - как быстро гаснет
                                # уже показанный пик между опросами.

    def read_vu(self, dt=0.1):
        """
        Реальный уровень ИГРАЮЩЕГО звука (пики сигнала) - НЕ системная
        громкость volume_pct, независимо от того, на что она выкручена -
        для VU-метра ленты (см. докстринг read_vu в пояснении к проекту).
        Источник - Core Audio IAudioMeterInformation, тот же механизм,
        которым Windows рисует пиковые индикаторы в системном микшере
        громкости - НЕ требует захвата аудиопотока (loopback), просто
        спрашивает у драйвера текущий пик с прошлого опроса.

        dt - секунд с прошлого вызова (для затухания показанного пика, см.
        VU_RELEASE_SECONDS) - вызывающий код (pc_hud.py) передаёт фактически
        прошедшее время между тиками главного цикла, не TICK_INTERVAL
        "в теории".

        Возвращает dict: vu_peak_pct (пик по всем каналам сразу, 0-100),
        vu_left_pct, vu_right_pct (раздельно для стерео; при ином числе
        каналов, включая моно, оба получают общий пик - честного разделения
        тогда всё равно нет).

        Нули (НЕ None) при недоступности - в отличие от read_state(), эти
        значения идут напрямую в common_metrics ленты (pc_hud.py), где
        всегда ожидается float (см. там же паттерн disk1/disk2 -> 0.0).
        """
        empty = {"vu_peak_pct": 0.0, "vu_left_pct": 0.0, "vu_right_pct": 0.0}
        if not self.available:
            return empty

        meter = self._meter_iface or self._get_meter_interface()
        if meter is None:
            return empty

        try:
            peak = meter.GetPeakValue()  # 0.0-1.0, по всем каналам сразу
            channel_count = meter.GetMeteringChannelCount()
            if channel_count >= 2:
                buf = (ctypes.c_float * channel_count)()
                # afPeakValues у _IAudioMeterInformationFull объявлен как
                # POINTER(c_float) с направлением "in" (буфер выделяем МЫ,
                # см. докстринг класса выше) - ctypes-массив передаётся как
                # есть, comtypes сам приводит его к указателю на первый
                # элемент при вызове.
                meter.GetChannelsPeakValues(channel_count, buf)
                left_raw, right_raw = buf[0], buf[1]
            else:
                left_raw = right_raw = peak
        except (COMError, AttributeError, OSError, ValueError) as e:
            # Расширенный except - НАМЕРЕННО шире, чем просто COMError:
            # на реальном первом запуске сюда прилетел AttributeError из-за
            # неполного объявления интерфейса в pycaw (см. докстринг
            # _IAudioMeterInformationFull) - он не ловился и убивал ВЕСЬ
            # поток metrics_main_loop целиком (не только VU), из-за чего
            # зависали вообще все метрики и лента/OLED переставали
            # обновляться. Раз это метод "не должен бросать исключений"
            # (см. докстринг read_vu про "нули, не None"), ловим тут любую
            # реалистичную причину сбоя чтения COM-интерфейса, а не только
            # ожидаемую COMError.
            print(f"[audio] read_vu failed, will reinit meter: {e}", flush=True)
            self._meter_iface = None
            return empty

        def _decay(prev_pct, new_raw):
            new_pct = max(0.0, min(100.0, new_raw * 100.0))
            if new_pct >= prev_pct or self.VU_RELEASE_SECONDS <= 0:
                return new_pct  # атака - мгновенно (или спад выключен)
            frac = min(1.0, dt / self.VU_RELEASE_SECONDS)
            return prev_pct + (new_pct - prev_pct) * frac

        self._vu_peak = _decay(self._vu_peak, peak)
        self._vu_left = _decay(self._vu_left, left_raw)
        self._vu_right = _decay(self._vu_right, right_raw)

        return {
            "vu_peak_pct": round(self._vu_peak, 1),
            "vu_left_pct": round(self._vu_left, 1),
            "vu_right_pct": round(self._vu_right, 1),
        }

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


# ---------------- Now Playing (SMTC - System Media Transport Controls) ----------------

class MediaMonitor:
    """
    Текущий трек через Windows SMTC - тот же источник данных, что у системного
    медиа-виджета (Win+K/уведомления). НЕ полагается на "текущую" сессию по
    версии Windows (это ненадёжная эвристика, часто даёт None при реально
    играющей музыке) - вместо этого перебирает ВСЕ зарегистрированные SMTC-
    сессии и берёт первую, где статус реально Playing (см. _read_async()).

    ВАЖНО: SMTC - это API, который каждое приложение регистрирует САМО.
    UWP-приложения на MediaPlayer (в т.ч. встроенный проигрыватель Windows -
    "Фильмы и ТВ"/Media Player, Groove) интегрируются автоматически. Обычные
    Win32-программы (в т.ч. многие сторонние плееры) должны реализовать это
    явно - если приложение НЕ добавляет свою сессию в SMTC, оно тут просто
    не появится, вне зависимости от того, насколько правильно написан этот
    класс. Известно, что так ведут себя некоторые версии AIMP (нужна
    отдельная настройка/плагин интеграции с системными медиаклавишами - см.
    настройки AIMP) и большинство десктоп-клиентов Plex (Electron-based, SMTC
    не реализуют вовсе). Если не уверены, работает ли конкретный плеер -
    проверьте системный медиа-виджет Win+K во время его воспроизведения:
    если плеер не появляется там - он не появится и тут, дело не в этом коде.

    winsdk - WinRT-биндинг, весь API асинхронный. Остальной pc_hud.py
    синхронный, а опрашивается это редко (раз в POLL_INTERVAL, как GPU/диски) -
    поэтому просто оборачиваем в asyncio.run() на каждый вызов read(), не
    заводя постоянный event loop. Если winsdk не установлен - тихо возвращает
    "пустые" значения, как GpuMonitor/AudioController без своих библиотек.
    """

    def __init__(self):
        self.available = _WINSDK_AVAILABLE
        if not self.available:
            print("[media] winsdk не установлен - Now Playing недоступен", flush=True)

    def read(self):
        """dict: media_title, media_artist (None, если сейчас ничего не играет
        - в частности на паузе/стопе тоже None, см. _read_async() ниже -
        чтобы OLED-экран Now Playing автоматически пропускался ротацией
        через общий механизм screens.build_active_screens(), как экраны
        net2/disk2 без настройки - см. докстринг screens.py), media_playing
        ('да'/'нет', всегда строка, не None)."""
        empty = {"media_title": None, "media_artist": None, "media_playing": "нет"}
        if not self.available:
            return empty
        try:
            return asyncio.run(self._read_async())
        except Exception as e:
            print(f"[media] read failed: {e}", flush=True)
            return empty

    def debug_list_sessions(self):
        """
        Диагностика для ручной проверки (см. самотест в конце файла:
        `python metrics_windows.py`) - НЕ используется в обычной работе
        pc_hud.py. Печатает КАЖДУЮ SMTC-сессию, которую видит Windows, с её
        источником (app_user_model_id) и статусом - позволяет отличить два
        разных случая:
          - сессий вообще нет / нужного плеера нет в списке -> само
            приложение не регистрируется в SMTC, это не чинится в этом коде
            (см. докстринг класса выше про AIMP/Plex)
          - сессия есть, но playback_status не Playing -> отладка тут, в
            _read_async()/read()
        """
        if not self.available:
            print("[media] winsdk недоступен")
            return
        asyncio.run(self._debug_list_sessions_async())

    async def _debug_list_sessions_async(self):
        manager = await _MediaManager.request_async()
        sessions = manager.get_sessions()
        if not sessions:
            print("[media] Windows не видит НИ ОДНОЙ SMTC-сессии сейчас")
            return
        for session in sessions:
            info = session.get_playback_info()
            status = info.playback_status if info else None
            source = session.source_app_user_model_id
            print(f"[media] сессия: source={source!r} playback_status={status}")

    async def _read_async(self):
        manager = await _MediaManager.request_async()

        # НЕ используем manager.get_current_session() - это эвристика Windows
        # "какую сессию считать текущей", и она нередко возвращает None даже
        # при реально играющей музыке (особенно если открыто несколько
        # источников звука одновременно, или фокус недавно переключался
        # между приложениями) - см. обсуждение в README. Вместо этого сами
        # перебираем ВСЕ зарегистрированные SMTC-сессии и берём первую, где
        # реально Playing - так надёжнее и не зависит от того, что Windows
        # сочла "текущим".
        sessions = manager.get_sessions()

        playing_session = None
        for session in sessions:
            info = session.get_playback_info()
            # PlaybackStatus: Closed=0, Opened=1, Changing=2, Stopped=3, Playing=4, Paused=5
            if info and info.playback_status == 4:
                playing_session = session
                break

        if playing_session is None:
            return {"media_title": None, "media_artist": None, "media_playing": "нет"}

        props = await playing_session.try_get_media_properties_async()
        title = (props.title or "").strip() if props else ""
        artist = (props.artist or "").strip() if props else ""

        return {
            "media_title": title or None,
            "media_artist": artist or None,
            "media_playing": "да",
        }


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
    print("VU:", audio.read_vu())

    media = MediaMonitor()
    print("Media:", media.read())
    print("Media sessions raw dump:")
    media.debug_list_sessions()
