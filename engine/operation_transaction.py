"""Request-local SQLite leases sharing one atomic operation transaction."""

from contextvars import ContextVar

current = ContextVar('operation_transaction', default=None)


class Transaction:
    def __init__(self, connection):
        self.connection = connection
        self.sequence = 0
        self.events = []

    def lease(self):
        self.sequence += 1
        return Lease(self.connection, f'operation_lease_{self.sequence}')


class Lease:
    """Local commits release savepoints; the operation owner commits the database."""

    def __init__(self, connection, name):
        self.connection = connection
        self.name = name
        self.active = False
        self.closed = False

    @property
    def in_transaction(self):
        return self.active

    def _begin(self):
        if self.closed:
            raise RuntimeError('Database lease is closed')
        if not self.active:
            self.connection.execute(f'SAVEPOINT {self.name}')
            self.active = True

    def execute(self, sql, parameters=()):
        self._begin()
        if sql.strip().upper() in ('BEGIN', 'BEGIN IMMEDIATE', 'BEGIN DEFERRED'):
            return self.connection.execute('SELECT 1')
        return self.connection.execute(sql, parameters)

    def executemany(self, sql, parameters):
        self._begin()
        return self.connection.executemany(sql, parameters)

    def commit(self):
        if self.active:
            self.connection.execute(f'RELEASE SAVEPOINT {self.name}')
            self.active = False

    def rollback(self):
        if self.active:
            self.connection.execute(f'ROLLBACK TO SAVEPOINT {self.name}')
            self.commit()

    def close(self):
        self.rollback()
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.rollback() if exc_type else self.commit()
