"""
bt_manager.py — BlueZ D-Bus backend for scan / pair / connect / trust.

Talks directly to bluetoothd over D-Bus (the same interface bluetoothctl
itself uses) rather than scripting the bluetoothctl text prompt, so results
are structured and don't depend on parsing human-readable terminal output.
"""

import threading
import queue

try:
    import dbus
    import dbus.mainloop.glib
    from gi.repository import GLib
    _DBUS_AVAILABLE = True
except ImportError:
    _DBUS_AVAILABLE = False

BLUEZ_SERVICE = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
OBJMGR_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

DBUS_CALL_TIMEOUT_S = 15


class BluetoothUnavailable(Exception):
    pass


class BTDevice:
    __slots__ = ("address", "name", "paired", "trusted", "connected", "rssi")

    def __init__(self, address, name=None, paired=False, trusted=False,
                 connected=False, rssi=None):
        self.address = address
        self.name = name or address
        self.paired = paired
        self.trusted = trusted
        self.connected = connected
        self.rssi = rssi

    def as_dict(self):
        return {
            "address": self.address, "name": self.name,
            "paired": self.paired, "trusted": self.trusted,
            "connected": self.connected, "rssi": self.rssi,
        }


class BluetoothManager:
    def __init__(self, adapter_path="/org/bluez/hci0"):
        if not _DBUS_AVAILABLE:
            raise BluetoothUnavailable(
                "dbus-python / PyGObject not installed. On the device, run: "
                "sudo apt install python3-dbus python3-gi"
            )
        self.adapter_path = adapter_path
        self._devices = {}
        self._devices_lock = threading.Lock()
        self._events = queue.Queue()
        self._scanning = False

        self._loop = None
        self._bus = None
        self._adapter = None
        self._adapter_props = None
        self._thread = None
        self._ready = threading.Event()
        self._start_error = None

    def start(self, timeout=5.0):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._thread = threading.Thread(target=self._run, daemon=True, name="bt-manager")
        self._thread.start()
        if not self._ready.wait(timeout):
            raise BluetoothUnavailable("Timed out connecting to BlueZ over D-Bus")
        if self._start_error:
            raise self._start_error

    def stop(self):
        if self._loop is not None:
            self._loop.quit()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self):
        try:
            self._bus = dbus.SystemBus()
            self._adapter = self._bus.get_object(BLUEZ_SERVICE, self.adapter_path)
            self._adapter_props = dbus.Interface(self._adapter, PROPS_IFACE)
            self._adapter_props.Set(ADAPTER_IFACE, "Powered", True)

            obj_mgr = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, "/"), OBJMGR_IFACE)
            for path, ifaces in obj_mgr.GetManagedObjects().items():
                if DEVICE_IFACE in ifaces:
                    self._update_device_from_props(path, ifaces[DEVICE_IFACE])

            self._bus.add_signal_receiver(
                self._on_interfaces_added, dbus_interface=OBJMGR_IFACE,
                signal_name="InterfacesAdded")
            self._bus.add_signal_receiver(
                self._on_properties_changed, dbus_interface=PROPS_IFACE,
                signal_name="PropertiesChanged", path_keyword="path")

            self._loop = GLib.MainLoop()
            self._ready.set()
            self._loop.run()
        except Exception as e:
            self._start_error = BluetoothUnavailable(str(e))
            self._ready.set()

    def _on_interfaces_added(self, path, interfaces):
        if DEVICE_IFACE in interfaces:
            self._update_device_from_props(path, interfaces[DEVICE_IFACE])

    def _on_properties_changed(self, interface, changed, invalidated, path=None):
        if interface == DEVICE_IFACE and path:
            self._update_device_from_props(path, changed, partial=True)

    def _update_device_from_props(self, path, props, partial=False):
        address = props.get("Address")
        if address is None:
            address = _address_from_path(path)
        if not address:
            return
        address = str(address)

        with self._devices_lock:
            existing = self._devices.get(address)
            name = str(props["Name"]) if "Name" in props else (existing.name if existing else None)
            paired = bool(props["Paired"]) if "Paired" in props else (existing.paired if existing else False)
            trusted = bool(props["Trusted"]) if "Trusted" in props else (existing.trusted if existing else False)
            connected = bool(props["Connected"]) if "Connected" in props else (existing.connected if existing else False)
            rssi = int(props["RSSI"]) if "RSSI" in props else (existing.rssi if existing else None)

            dev = BTDevice(address, name, paired, trusted, connected, rssi)
            self._devices[address] = dev

        self._events.put(("device_updated", dev.as_dict()))

    def get_discovered_devices(self):
        with self._devices_lock:
            return [d.as_dict() for d in self._devices.values()]

    def get_paired_devices(self):
        with self._devices_lock:
            return [d.as_dict() for d in self._devices.values() if d.paired]

    def is_scanning(self):
        return self._scanning

    def get_events(self):
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                break
        return out

    def start_scan(self):
        GLib.idle_add(self._do_start_scan)

    def stop_scan(self):
        GLib.idle_add(self._do_stop_scan)

    def pair(self, address):
        GLib.idle_add(self._do_pair, address)

    def connect(self, address):
        GLib.idle_add(self._do_connect, address)

    def disconnect(self, address):
        GLib.idle_add(self._do_disconnect, address)

    def trust(self, address):
        GLib.idle_add(self._do_trust, address)

    def remove(self, address):
        GLib.idle_add(self._do_remove, address)

    def _do_start_scan(self):
        try:
            iface = dbus.Interface(self._adapter, ADAPTER_IFACE)
            try:
                iface.SetDiscoveryFilter({"Transport": "auto"})
            except Exception:
                pass
            iface.StartDiscovery()
            self._scanning = True
            self._events.put(("scan_started", None))
        except Exception as e:
            self._events.put(("error", f"start_scan failed: {e}"))
        return False

    def _do_stop_scan(self):
        try:
            iface = dbus.Interface(self._adapter, ADAPTER_IFACE)
            iface.StopDiscovery()
        except Exception as e:
            self._events.put(("error", f"stop_scan failed: {e}"))
        finally:
            self._scanning = False
            self._events.put(("scan_stopped", None))
        return False

    def _device_path(self, address):
        return f"{self.adapter_path}/dev_{address.replace(':', '_')}"

    def _do_pair(self, address):
        try:
            path = self._device_path(address)
            dev = self._bus.get_object(BLUEZ_SERVICE, path)
            iface = dbus.Interface(dev, DEVICE_IFACE)
            iface.Pair(
                reply_handler=lambda: self._events.put(("pair_result", {"address": address, "ok": True})),
                error_handler=lambda e: self._events.put(("pair_result", {"address": address, "ok": False, "error": str(e)})),
                timeout=DBUS_CALL_TIMEOUT_S,
            )
        except Exception as e:
            self._events.put(("pair_result", {"address": address, "ok": False, "error": str(e)}))
        return False

    def _do_connect(self, address):
        try:
            path = self._device_path(address)
            dev = self._bus.get_object(BLUEZ_SERVICE, path)
            iface = dbus.Interface(dev, DEVICE_IFACE)
            iface.Connect(
                reply_handler=lambda: self._events.put(("connect_result", {"address": address, "ok": True})),
                error_handler=lambda e: self._events.put(("connect_result", {"address": address, "ok": False, "error": str(e)})),
                timeout=DBUS_CALL_TIMEOUT_S,
            )
        except Exception as e:
            self._events.put(("connect_result", {"address": address, "ok": False, "error": str(e)}))
        return False

    def _do_disconnect(self, address):
        try:
            path = self._device_path(address)
            dev = self._bus.get_object(BLUEZ_SERVICE, path)
            iface = dbus.Interface(dev, DEVICE_IFACE)
            iface.Disconnect(
                reply_handler=lambda: self._events.put(("disconnect_result", {"address": address, "ok": True})),
                error_handler=lambda e: self._events.put(("disconnect_result", {"address": address, "ok": False, "error": str(e)})),
                timeout=DBUS_CALL_TIMEOUT_S,
            )
        except Exception as e:
            self._events.put(("disconnect_result", {"address": address, "ok": False, "error": str(e)}))
        return False

    def _do_trust(self, address):
        try:
            path = self._device_path(address)
            dev = self._bus.get_object(BLUEZ_SERVICE, path)
            props = dbus.Interface(dev, PROPS_IFACE)
            props.Set(DEVICE_IFACE, "Trusted", True)
            self._events.put(("trust_result", {"address": address, "ok": True}))
        except Exception as e:
            self._events.put(("trust_result", {"address": address, "ok": False, "error": str(e)}))
        return False

    def _do_remove(self, address):
        try:
            path = self._device_path(address)
            iface = dbus.Interface(self._adapter, ADAPTER_IFACE)
            iface.RemoveDevice(path)
            with self._devices_lock:
                self._devices.pop(address, None)
            self._events.put(("remove_result", {"address": address, "ok": True}))
        except Exception as e:
            self._events.put(("remove_result", {"address": address, "ok": False, "error": str(e)}))
        return False


def _address_from_path(path):
    try:
        tail = str(path).rsplit("dev_", 1)[1]
        return tail.replace("_", ":")
    except Exception:
        return None
