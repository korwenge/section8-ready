"""Read and write CCLocalLevels.dat -- the save file behind the Create tab.

Windows: the file is XOR-11'd, then base64, then gzip, then a GD-flavoured
XML plist. macOS: the file is AES-256-ECB encrypted plain XML (needs the
`cryptography` package).

Always close Geometry Dash before writing: the game rewrites this file on
exit and would throw away our changes.
"""

import base64
import gzip
import os
import platform
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from gd_api import analyze_level_string, b64decode_text, decode_level_string

MAC_KEY = b"ipu9TUv54yv]isFMh5@;t.5w34E2Ry@{"

# Default empty-level header: background/ground colours plus basic settings.
DEFAULT_HEADER = (
    "kS38,1_40_2_125_3_255_11_255_12_255_13_255_4_-1_6_1000_7_1_15_1_18_0_8_1|"
    "1_0_2_102_3_255_11_255_12_255_13_255_4_-1_6_1001_7_1_15_1_18_0_8_1|,"
    "kA13,0,kA15,0,kA16,0,kA14,,kA6,0,kA7,0,kA17,0,kA18,0,kS39,0,"
    "kA2,0,kA3,0,kA8,0,kA4,0,kA9,0,kA10,0,kA11,0;"
)


def default_save_path():
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "GeometryDash" / "CCLocalLevels.dat"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "GeometryDash" / "CCLocalLevels.dat"
    # Linux: Steam + Proton keeps the Windows layout inside the prefix.
    return (Path.home() / ".steam" / "steam" / "steamapps" / "compatdata" / "322170" / "pfx"
            / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / "GeometryDash"
            / "CCLocalLevels.dat")


BASE64_CHARS = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_+/=")


def _is_mac_format(data):
    # Windows saves are XOR-11'd base64, so every byte decodes to a base64 char;
    # AES output is random bytes and won't pass this for a whole 64-byte block.
    return not all((b ^ 11) in BASE64_CHARS for b in data[:64])


def decrypt(data):
    if _is_mac_format(data):
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            sys.exit("macOS saves need the 'cryptography' package: pip install cryptography")
        decryptor = Cipher(algorithms.AES(MAC_KEY), modes.ECB()).decryptor()
        plain = decryptor.update(data) + decryptor.finalize()
        return plain[:-plain[-1]].decode("utf-8")
    text = bytes(b ^ 11 for b in data).decode("ascii", "ignore").rstrip("\0")
    return gzip.decompress(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))).decode("utf-8")


def encrypt(xml_text, mac=False):
    if mac:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        raw = xml_text.encode("utf-8")
        pad = 16 - len(raw) % 16
        encryptor = Cipher(algorithms.AES(MAC_KEY), modes.ECB()).encryptor()
        return encryptor.update(raw + bytes([pad]) * pad) + encryptor.finalize()
    encoded = base64.urlsafe_b64encode(gzip.compress(xml_text.encode("utf-8")))
    return bytes(b ^ 11 for b in encoded)


def encode_level_string(level_string):
    return base64.urlsafe_b64encode(gzip.compress(level_string.encode("utf-8"))).decode("ascii")


# --- GD plist <-> Python -------------------------------------------------------

def _value(elem):
    if elem.tag == "d":
        return _dict(elem)
    if elem.tag == "t":
        return True
    if elem.tag == "i":
        return int(elem.text or 0)
    if elem.tag == "r":
        return float(elem.text or 0)
    return elem.text or ""


def _dict(elem):
    children = list(elem)
    return {k.text: _value(v) for k, v in zip(children[0::2], children[1::2])}


def _to_elem(tag_parent, value, dict_tag="d"):
    if isinstance(value, dict):
        d = ET.SubElement(tag_parent, dict_tag)
        for k, v in value.items():
            ET.SubElement(d, "k").text = k
            _to_elem(d, v)
    elif value is True:
        ET.SubElement(tag_parent, "t")
    elif isinstance(value, int):
        ET.SubElement(tag_parent, "i").text = str(value)
    elif isinstance(value, float):
        ET.SubElement(tag_parent, "r").text = repr(value)
    else:
        ET.SubElement(tag_parent, "s").text = str(value)


class LocalLevels:
    """The levels in your Create tab, in the order the game shows them."""

    def __init__(self, path=None):
        self.path = Path(path) if path else default_save_path()
        if not self.path.exists():
            raise FileNotFoundError(f"No save file at {self.path} (use --save to point at it)")
        data = self.path.read_bytes()
        self.mac = _is_mac_format(data)
        root = ET.fromstring(decrypt(data))
        self.plist_attrs = root.attrib
        self.top = _dict(root.find("dict"))
        llm = self.top.get("LLM_01", {})
        self.levels = [llm[k] for k in sorted((k for k in llm if k.startswith("k_")),
                                              key=lambda k: int(k[2:]))]
        self._backed_up = False

    # -- reading --

    def find(self, name):
        name_l = name.lower()
        exact = [lvl for lvl in self.levels if lvl.get("k2", "").lower() == name_l]
        if exact:
            return exact[0]
        partial = [lvl for lvl in self.levels if name_l in lvl.get("k2", "").lower()]
        if not partial:
            raise KeyError(f"No level named '{name}' in your Create tab")
        return partial[0]

    def summaries(self):
        return [{
            "name": lvl.get("k2", ""),
            "description": b64decode_text(lvl.get("k3", "")),
            "version": lvl.get("k16", 1),
            "objects": lvl.get("k48", "?"),
            "verified": bool(lvl.get("k14")),
            "uploaded_id": lvl.get("k1"),
        } for lvl in self.levels]

    def level_string(self, level):
        data = level.get("k4", "")
        return decode_level_string(data) if data else DEFAULT_HEADER

    def stats(self, level):
        return analyze_level_string(self.level_string(level))

    # -- writing --

    def _creator_name(self):
        for lvl in self.levels:
            if lvl.get("k5"):
                return lvl["k5"]
        return "Player"

    def create_level(self, name, description, object_strings):
        level_string = DEFAULT_HEADER + ";".join(object_strings) + (";" if object_strings else "")
        level = {
            "kCEK": 4,
            "k2": name,
            "k3": base64.urlsafe_b64encode(description.encode()).decode() if description else "",
            "k4": encode_level_string(level_string),
            "k5": self._creator_name(),
            "k13": True,
            "k21": 2,
            "k16": 1,
            "k80": 0,
            "k50": 45,
            "k48": len(object_strings),
        }
        self.levels.insert(0, level)  # new levels show up at the top of the list
        return level

    def append_objects(self, level, object_strings):
        level_string = self.level_string(level).rstrip(";") + ";" + ";".join(object_strings) + ";"
        level["k4"] = encode_level_string(level_string)
        level["k48"] = int(level.get("k48") or 0) + len(object_strings)
        level["k16"] = int(level.get("k16") or 1)

    def save(self):
        if not self._backed_up:
            backup = self.path.with_name(f"{self.path.name}.backup-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(self.path, backup)
            self._backed_up = True
            print(f"  (backed up your original save to {backup})")
        llm = {k: v for k, v in self.top.get("LLM_01", {}).items() if not k.startswith("k_")}
        llm.setdefault("_isArr", True)
        for i, lvl in enumerate(self.levels):
            llm[f"k_{i}"] = lvl
        self.top["LLM_01"] = llm

        root = ET.Element("plist", self.plist_attrs or {"version": "1.0", "gjver": "2.0"})
        _to_elem(root, self.top, dict_tag="dict")
        xml_text = '<?xml version="1.0"?>' + ET.tostring(root, encoding="unicode")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(encrypt(xml_text, self.mac))
        tmp.replace(self.path)
