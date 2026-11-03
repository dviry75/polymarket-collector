import multiprocessing

import pytest

from demo_v2.fences import AtomicFenceSlots


def _read_and_update(slots,connection):
    connection.send(slots[1])
    slots[0]=2**63+123
    connection.close()


def test_atomic_slots_share_values_across_spawn_without_shared_process_mutex():
    slots=AtomicFenceSlots(2);slots[1]=2**64-1
    context=multiprocessing.get_context('spawn');parent,child=context.Pipe()
    process=context.Process(target=_read_and_update,args=(slots,child))
    process.start();child.close()
    assert parent.poll(10) and parent.recv()==2**64-1
    process.join(10);parent.close()
    assert process.exitcode==0 and slots[0]==2**63+123
    assert not hasattr(slots,'get_lock')
    with pytest.raises(IndexError):slots[-1]
    with pytest.raises(ValueError):slots[0]=-1
    with pytest.raises(ValueError):slots[0]=2**64
