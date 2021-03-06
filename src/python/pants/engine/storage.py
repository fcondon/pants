# coding=utf-8
# Copyright 2016 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import cPickle as pickle
import cStringIO as StringIO
import sys
from abc import abstractmethod
from binascii import hexlify
from collections import Counter
from contextlib import closing
from hashlib import sha1
from struct import Struct as StdlibStruct

import lmdb
import six

from pants.engine.nodes import State
from pants.engine.objects import Closable, SerializationError
from pants.util.dirutil import safe_mkdtemp
from pants.util.meta import AbstractClass


def _unpickle(value):
  if isinstance(value, six.binary_type):
    # Deserialize string values.
    return pickle.loads(value)
  # Deserialize values with file interface,
  return pickle.load(value)


def _identity(value):
  return value


def _copy_bytes(value):
  return bytes(value)


class Key(object):
  """Holds the digest for the object, which uniquely identifies it.

  The `_hash` is a memoized 32 bit integer hashcode computed from the digest.
  """

  __slots__ = ['_digest', '_hash']

  # The digest implementation used for Keys.
  _DIGEST_IMPL = sha1
  _DIGEST_SIZE = _DIGEST_IMPL().digest_size

  # A struct.Struct definition for grabbing the first 4 bytes off of a digest of
  # size DIGEST_SIZE, and discarding the rest.
  _32_BIT_STRUCT = StdlibStruct(b'<l' + (b'x' * (_DIGEST_SIZE - 4)))

  @classmethod
  def create(cls, blob):
    """Given a blob, hash it to construct a Key.

    :param blob: Binary content to hash.
    :param type_: Type of the object to be hashed.
    """
    return cls.create_from_digest(cls._DIGEST_IMPL(blob).digest())

  @classmethod
  def create_from_digest(cls, digest):
    """Given the digest for a key, create a Key.

    :param digest: The digest for the Key.
    """
    hash_ = cls._32_BIT_STRUCT.unpack(digest)[0]
    return cls(digest, hash_)

  def __init__(self, digest, hash_):
    """Not for direct use: construct a Key via `create` instead."""
    self._digest = digest
    self._hash = hash_

  @property
  def digest(self):
    return self._digest

  def __hash__(self):
    return self._hash

  def __eq__(self, other):
    return self._digest == other._digest

  def __ne__(self, other):
    return not (self == other)

  def __repr__(self):
    return 'Key({})'.format(hexlify(self._digest))

  def __str__(self):
    return repr(self)


class InvalidKeyError(Exception):
  """Indicate an invalid `Key` entry"""


class Storage(Closable):
  """Stores and creates unique keys for input pickleable objects.

  Storage as `Closable`, `close()` can be called either explicitly or through the `with`
  statement in a context.

  Besides contents indexed by their hashed Keys, a secondary index is also provided
  for mappings between Keys. This allows to establish links between contents that
  are represented by those keys. Cache for example is such a use case.

  Convenience methods to translate nodes and states in
  `pants.engine.scheduler.StepRequest` and `pants.engine.scheduler.StepResult`
  into keys, and vice versa are also provided.
  """

  LMDB_KEY_MAPPINGS_DB_NAME = b'_key_mappings_'

  @classmethod
  def create(cls, path=None, in_memory=True, protocol=None):
    """Create a content addressable Storage backed by a key value store.

    :param path: If in_memory=False, the path to store the database in.
    :param in_memory: Indicate whether to use the in-memory kvs or an embeded database.
    :param protocol: Serialization protocol for pickle, if not provided will use ASCII protocol.
    """
    if in_memory:
      content, key_mappings = InMemoryDb(), InMemoryDb()
    else:
      content, key_mappings = Lmdb.create(path=path,
                                          child_databases=[cls.LMDB_KEY_MAPPINGS_DB_NAME])

    return Storage(content, key_mappings, protocol=protocol)

  @classmethod
  def clone(cls, storage):
    """Clone a Storage so it can be shared across process boundary."""
    if isinstance(storage._contents, InMemoryDb):
      contents, key_mappings = storage._contents, storage._key_mappings
    else:
      contents, key_mappings = Lmdb.create(path=storage._contents.path,
                                           child_databases=[cls.LMDB_KEY_MAPPINGS_DB_NAME])

    return Storage(contents, key_mappings, protocol=storage._protocol)

  def __init__(self, contents, key_mappings, protocol=None):
    """Not for direct use: construct a Storage via either `create` or `clone`."""
    self._contents = contents
    self._key_mappings = key_mappings
    self._protocol = protocol if protocol is not None else pickle.HIGHEST_PROTOCOL
    self._memo = dict()

  def put(self, obj):
    """Serialize and hash something pickleable, returning a unique key to retrieve it later.

    NB: pickle by default memoizes objects by id and pickle repeated objects by references,
    for example, (A, A) uses less space than (A, A'), A and A' are equal but not identical.
    For content addressability we need equality. Use `fast` mode to turn off memo.
    Longer term see https://github.com/pantsbuild/pants/issues/2969
    """
    try:
      with closing(StringIO.StringIO()) as buf:
        pickler = pickle.Pickler(buf, protocol=self._protocol)
        pickler.fast = 1
        pickler.dump(obj)
        blob = buf.getvalue()

        # Hash the blob and store it if it does not exist.
        key = Key.create(blob)
        if key not in self._memo:
          self._memo[key] = obj
          self._contents.put(key.digest, blob)
    except Exception as e:
      # Unfortunately, pickle can raise things other than PickleError instances.  For example it
      # will raise ValueError when handed a lambda; so we handle the otherwise overly-broad
      # `Exception` type here.
      raise SerializationError('Failed to pickle {}: {}'.format(obj, e), e)

    return key

  def get(self, key):
    """Given a key, return its deserialized content.

    Note that since this is not a cache, if we do not have the content for the object, this
    operation fails noisily.
    """
    if not isinstance(key, Key):
      raise InvalidKeyError('Not a valid key: {}'.format(key))

    obj = self._memo.get(key)
    if obj is not None:
      return obj
    return self._contents.get(key.digest, _unpickle)

  def put_state(self, state):
    """Put the components of the State individually in storage, then put the aggregate."""
    return self.put(tuple(self.put(r).digest for r in state.to_components()))

  def get_state(self, state_key):
    """The inverse of put_state: get a State given its Key."""
    return State.from_components(tuple(self.get(Key.create_from_digest(d)) for d in self.get(state_key)))

  def add_mapping(self, from_key, to_key):
    """Establish one to one relationship from one Key to another Key.

    Content that keys represent should either already exist or the caller must
    check for existence.

    Unlike content storage, key mappings allows overwriting existing entries,
    meaning a key can be re-mapped to a different key.
    """
    self._key_mappings.put(key=from_key.digest,
                           value=pickle.dumps(to_key, protocol=self._protocol))

  def get_mapping(self, from_key):
    """Retrieve the mapping Key from a given Key.

    Noe is returned if the mapping does not exist.
    """
    to_key = self._key_mappings.get(key=from_key.digest)

    if to_key is None:
      return None

    if isinstance(to_key, six.binary_type):
      return pickle.loads(to_key)
    return pickle.load(to_key)

  def close(self):
    self._contents.close()

  def _assert_type_matches(self, value, key_type):
    """Ensure the type of deserialized object matches the type from key."""
    value_type = type(value)
    if key_type and value_type is not key_type:
      raise ValueError('Mismatch types, key: {}, value: {}'
                       .format(key_type, value_type))
    return value


class Cache(Closable):
  """Cache the State resulting from a given Runnable."""

  @classmethod
  def create(cls, storage=None, cache_stats=None):
    """Create a Cache from a given storage instance."""

    storage = storage or Storage.create()
    cache_stats = cache_stats or CacheStats()
    return Cache(storage, cache_stats)

  def __init__(self, storage, cache_stats):
    """Initialize the cache. Not for direct use, use factory methods `create`.

    :param storage: Main storage for all requests and results.
    :param cache_stats: Stats for hits and misses.
    """
    self._storage = storage
    self._cache_stats = cache_stats

  def get(self, runnable):
    """Get the request key and hopefully a cached result for a given Runnable."""
    request_key = self._storage.put_state(runnable)
    return request_key, self.get_for_key(request_key)

  def get_for_key(self, request_key):
    """Given a request_key (for a Runnable), get the cached result."""
    result_key = self._storage.get_mapping(request_key)
    if result_key is None:
      self._cache_stats.add_miss()
      return None

    self._cache_stats.add_hit()
    return self._storage.get(result_key)

  def put(self, request_key, result):
    """Save the State for a given Runnable and return a key for the result."""
    result_key = self._storage.put(result)
    self.put_for_key(request_key, result_key)
    return result_key

  def put_for_key(self, request_key, result_key):
    """Save the State for a given Runnable and return a key for the result."""
    self._storage.add_mapping(from_key=request_key, to_key=result_key)

  def get_stats(self):
    return self._cache_stats

  def items(self):
    """Iterate over all cached request, result for testing purpose."""
    for digest, _ in self._storage._key_mappings.items():
      request_key = Key.create_from_digest(digest)
      request = self._storage.get(request_key)
      yield request, self._storage.get(self._storage.get_mapping(self._storage.put(request)))

  def close(self):
    # NB: This is a facade above a Storage instance, which is always closed independently.
    pass


class CacheStats(Counter):
  """Record cache hits and misses."""

  HIT_KEY = 'hits'
  MISS_KEY = 'misses'

  def add_hit(self):
    """Increment hit count by 1."""
    self[self.HIT_KEY] += 1

  def add_miss(self):
    """Increment miss count by 1."""
    self[self.MISS_KEY] += 1

  @property
  def hits(self):
    """Raw count for hits."""
    return self[self.HIT_KEY]

  @property
  def misses(self):
    """Raw count for misses."""
    return self[self.MISS_KEY]

  @property
  def total(self):
    """Total count including hits and misses."""
    return self[self.HIT_KEY] + self[self.MISS_KEY]

  def __repr__(self):
    return 'hits={}, misses={}, total={}'.format(self.hits, self.misses, self.total)


class KeyValueStore(Closable, AbstractClass):
  @abstractmethod
  def get(self, key, transform=_identity):
    """Fetch the value for a given key.

    :param key: key in bytestring.
    :param transform: optional function that is applied on the retrieved value from storage
      before it is returned, since the original value may be only valid within the context.
    :return: value can be either string-like or file-like, `None` if does not exist.
    """

  @abstractmethod
  def put(self, key, value, transform=_copy_bytes):
    """Save the value under a key, but only once.

    The write once semantics is specifically provided for the content addressable use case.

    :param key: key in bytestring.
    :param value: value in bytestring.
    :param transform: optional function that is applied on the input value before it is
      saved to the storage, since the original value may be only valid within the context,
      default is to play safe and make a copy.
    :return: `True` to indicate the write actually happens, i.e, first write, `False` for
      repeated writes of the same key.
    """

  @abstractmethod
  def items(self):
    """Generator to iterate over items.

    For testing purpose.
    """


class InMemoryDb(KeyValueStore):
  """An in-memory implementation of the kvs interface."""

  def __init__(self):
    self._storage = dict()

  def get(self, key, transform=_identity):
    return transform(self._storage.get(key))

  def put(self, key, value, transform=_copy_bytes):
    if key in self._storage:
      return False
    self._storage[key] = transform(value)
    return True

  def items(self):
    for k in iter(self._storage):
      yield k, self._storage.get(k)


class Lmdb(KeyValueStore):
  """A lmdb implementation of the kvs interface."""

  # TODO make this more configurable through a subsystem.

  # 256GB - some arbitrary maximum size database may grow to.
  MAX_DATABASE_SIZE = 256 * 1024 * 1024 * 1024

  # writemap will use a writeable memory mapping to directly update storage, therefore
  # improves performance. But it may cause filesystems that don’t support sparse files,
  # such as OSX, to immediately preallocate map_size = bytes of underlying storage.
  # See https://lmdb.readthedocs.org/en/release/#writemap-mode
  USE_SPARSE_FILES = sys.platform != 'darwin'

  @classmethod
  def create(self, path=None, child_databases=None):
    """
    :param path: Database directory location, if `None` a temporary location will be provided
      and cleaned up upon process exit.
    :param child_databases: Optional child database names.
    :return: List of Lmdb databases, main database under the path is always created,
     plus the child databases requested.
    """
    path = path if path is not None else safe_mkdtemp()
    child_databases = child_databases or []
    env = lmdb.open(path, map_size=self.MAX_DATABASE_SIZE,
                    metasync=False, sync=False, map_async=True,
                    writemap=self.USE_SPARSE_FILES,
                    max_dbs=1+len(child_databases))
    instances = [Lmdb(env)]
    for child_db in child_databases:
      instances.append(Lmdb(env, env.open_db(child_db)))
    return tuple(instances)

  def __init__(self, env, db=None):
    """Not for direct use, use factory method `create`.

    db if None represents the main database.
    """
    self._env = env
    self._db = db

  @property
  def path(self):
    return self._env.path()

  def get(self, key, transform=_identity):
    """Return the value or `None` if the key does not exist.

    NB: Memory mapped storage returns a buffer object without copying keys or values, which
    is then wrapped with `StringIO` as the more friendly string buffer to allow `pickle.load`
    to read, again no copy involved.
    """
    with self._env.begin(db=self._db, buffers=True) as txn:
      value = txn.get(key)
      if value is not None:
        return transform(StringIO.StringIO(value))
      return None

  def put(self, key, value, transform=_identity):
    """Returning True if the key/value are actually written to the storage.

    No need to do additional transform since value is to be persisted.
    """
    with self._env.begin(db=self._db, buffers=True, write=True) as txn:
      return txn.put(key, transform(value), overwrite=False)

  def items(self):
    with self._env.begin(db=self._db, buffers=True) as txn:
      cursor = txn.cursor()
      for k, v in cursor:
        yield k, v

  def close(self):
    """Close the lmdb environment, calling multiple times has no effect."""
    self._env.close()
