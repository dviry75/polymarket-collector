"""Stable human trade numbers in existing DEMO metadata; UUIDs remain identity.

All allocation/backfill callers hold the same BEGIN IMMEDIATE transaction as
position insertion. The high-water mark survives both kinds of explicit reset.
No read endpoint allocates or renumbers trades and no schema change is needed.
"""
PREFIX = 'trade_serial:'
COUNTER = 'trade_serial_counter'
MAX_SERIAL = 9007199254740991


def positive(value):
    result=int(value)
    if not 1<=result<=MAX_SERIAL or str(result)!=str(value):
        raise ValueError('invalid DEMO trade serial')
    return result


def number(conn,position_id):
    row=conn.execute('SELECT value FROM demo_v2_meta WHERE key=?',(PREFIX+position_id,)).fetchone()
    if row is None:raise ValueError('DEMO trade serial has not been initialized')
    return positive(row[0])


def ensure(conn):
    """Assign only missing historical numbers, oldest entry first, UUID for ties."""
    stored=conn.execute('SELECT value FROM demo_v2_meta WHERE key=?',(COUNTER,)).fetchone()
    assigned=[positive(row[0]) for row in conn.execute('SELECT value FROM demo_v2_meta WHERE key GLOB ?',(PREFIX+'*',))]
    if len(assigned)!=len(set(assigned)):raise ValueError('duplicate DEMO trade serial')
    current=int(stored[0]) if stored else max(assigned,default=0)
    if current<max(assigned,default=0):raise ValueError('DEMO trade serial counter moved backwards')
    if not 0<=current<=MAX_SERIAL:raise ValueError('invalid DEMO trade serial counter')
    missing=conn.execute('''SELECT p.id FROM demo_v2_positions p
        LEFT JOIN demo_v2_meta m ON m.key=?||p.id WHERE m.key IS NULL
        ORDER BY p.entry_at,p.id''',(PREFIX,)).fetchall()
    for row in missing:
        current=positive(str(current+1))
        conn.execute('INSERT INTO demo_v2_meta(key,value) VALUES (?,?)',(PREFIX+row['id'],str(current)))
    if missing or stored is None:
        conn.execute('INSERT INTO demo_v2_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (COUNTER,str(current)))


def allocate(conn,position_id):
    row=conn.execute('SELECT value FROM demo_v2_meta WHERE key=?',(COUNTER,)).fetchone()
    if row is None:raise ValueError('DEMO trade serial counter has not been initialized')
    value=positive(str(int(row[0])+1))
    conn.execute('UPDATE demo_v2_meta SET value=? WHERE key=?',(str(value),COUNTER))
    conn.execute('INSERT INTO demo_v2_meta(key,value) VALUES (?,?)',(PREFIX+position_id,str(value)))
    return value


def view(conn,row):
    return {**dict(row),'serial_number':number(conn,row['id'])}
