"""Lock-free process fencing, without a crash-sensitive shared semaphore.

RawArray supplies storage only; its ordinary Python accesses are NOT used for
synchronization. GCC libatomic performs checked lock-free uint64 atomic loads and
stores with sequential consistency. No pointers are sent between processes: each
process computes its own address in the inherited shared allocation.
"""
import ctypes
from functools import lru_cache
import multiprocessing
import sys


@lru_cache(maxsize=1)
def _operations():
    if sys.platform!='linux':
        raise RuntimeError('DEMO process fencing requires Linux libatomic')
    try:
        library=ctypes.CDLL('libatomic.so.1')
        load=getattr(library,'__atomic_load_8')
        store=getattr(library,'__atomic_store_8')
        lock_free=getattr(library,'__atomic_is_lock_free')
    except (OSError,AttributeError) as exc:
        raise RuntimeError('DEMO process fencing requires libatomic.so.1') from exc
    load.argtypes=(ctypes.c_void_p,ctypes.c_int);load.restype=ctypes.c_uint64
    store.argtypes=(ctypes.c_void_p,ctypes.c_uint64,ctypes.c_int);store.restype=None
    lock_free.argtypes=(ctypes.c_size_t,ctypes.c_void_p);lock_free.restype=ctypes.c_bool
    return load,store,lock_free


class AtomicFenceSlots:
    def __init__(self, count):
        if count<1:
            raise ValueError('fence capacity must be positive')
        self.storage=multiprocessing.get_context('spawn').RawArray('Q',count)
        address=ctypes.addressof(self.storage)
        if ctypes.sizeof(ctypes.c_uint64)!=8 or address%8 or not _operations()[2](8,address):
            raise RuntimeError('aligned lock-free 64-bit atomic fences are unavailable')

    def __len__(self):
        return len(self.storage)

    def _address(self,index):
        if not isinstance(index,int) or not 0<=index<len(self):
            raise IndexError('invalid fence slot')
        return ctypes.addressof(self.storage)+index*8

    def __getitem__(self,index):
        return _operations()[0](self._address(index),5)  # __ATOMIC_SEQ_CST

    def __setitem__(self,index,value):
        if not isinstance(value,int) or not 0<=value<2**64:
            raise ValueError('invalid fence epoch')
        _operations()[1](self._address(index),value,5)
