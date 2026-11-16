import ctypes as c
from ctypes import wintypes as w


class CREDENTIAL(c.Structure):
    _fields_ = [("Flags", w.DWORD), ("Type", w.DWORD), ("TargetName", w.LPWSTR),
                ("Comment", w.LPWSTR), ("LastWritten", w.FILETIME),
                ("CredentialBlobSize", w.DWORD), ("CredentialBlob", c.POINTER(c.c_ubyte)),
                ("Persist", w.DWORD), ("AttributeCount", w.DWORD),
                ("Attributes", c.c_void_p), ("TargetAlias", w.LPWSTR), ("UserName", w.LPWSTR)]


class Credentials:
    """API keys are kept in this Windows user's Credential Manager."""

    def __init__(self, namespace="CodePilot"):
        self.namespace = namespace
        self.api = c.WinDLL("advapi32", use_last_error=True)
        self.api.CredWriteW.argtypes = [c.POINTER(CREDENTIAL), w.DWORD]
        self.api.CredWriteW.restype = w.BOOL
        self.api.CredReadW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.POINTER(c.POINTER(CREDENTIAL))]
        self.api.CredReadW.restype = w.BOOL
        self.api.CredDeleteW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD]
        self.api.CredDeleteW.restype = w.BOOL
        self.api.CredFree.argtypes = [c.c_void_p]

    def target(self, profile_id):
        return f"{self.namespace}/{profile_id}"

    def get(self, profile_id):
        ptr = c.POINTER(CREDENTIAL)()
        if not self.api.CredReadW(self.target(profile_id), 1, 0, c.byref(ptr)):
            error = c.get_last_error()
            if error == 1168:
                return ""
            raise c.WinError(error)
        try:
            item = ptr.contents
            return c.string_at(item.CredentialBlob, item.CredentialBlobSize).decode("utf-16-le")
        finally:
            self.api.CredFree(ptr)

    def set(self, profile_id, key):
        blob = key.encode("utf-16-le")
        if len(blob) > 2560:
            raise ValueError("API Key 超过 Windows 凭据允许的长度")
        raw = (c.c_ubyte * len(blob)).from_buffer_copy(blob)
        item = CREDENTIAL(Type=1, TargetName=self.target(profile_id),
                          CredentialBlobSize=len(blob), CredentialBlob=raw,
                          Persist=2, UserName="CodePilot")
        if not self.api.CredWriteW(c.byref(item), 0):
            raise c.WinError(c.get_last_error())

    def delete(self, profile_id):
        if not self.api.CredDeleteW(self.target(profile_id), 1, 0):
            if c.get_last_error() != 1168:
                raise c.WinError(c.get_last_error())
