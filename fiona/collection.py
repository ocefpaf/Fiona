# -*- coding: utf-8 -*-
# Collections provide file-like access to feature data

import logging
import os
import warnings

from fiona import compat, vfs
from fiona.ogrext import Iterator, ItemsIterator, KeysIterator
from fiona.ogrext import Session, WritingSession
from fiona.ogrext import (
    calc_gdal_version_num, get_gdal_version_num, get_gdal_release_name)
from fiona.ogrext import buffer_to_virtual_file, remove_virtual_file
from fiona.errors import DriverError, SchemaError, CRSError
from fiona._drivers import driver_count, GDALEnv
from fiona.drvsupport import supported_drivers, AWSGDALEnv
from six import string_types, binary_type


log = logging.getLogger(__name__)


class Collection(object):

    """A file-like interface to features of a vector dataset

    Python text file objects are iterators over lines of a file. Fiona
    Collections are similar iterators (not lists!) over features
    represented as GeoJSON-like mappings.
    """

    def __init__(self, path, mode='r', driver=None, schema=None, crs=None,
                 encoding=None, layer=None, vsi=None, archive=None,
                 enabled_drivers=None, crs_wkt=None, **kwargs):

        """The required ``path`` is the absolute or relative path to
        a file, such as '/data/test_uk.shp'. In ``mode`` 'r', data can
        be read only. In ``mode`` 'a', data can be appended to a file.
        In ``mode`` 'w', data overwrites the existing contents of
        a file.

        In ``mode`` 'w', an OGR ``driver`` name and a ``schema`` are
        required. A Proj4 ``crs`` string is recommended. If both ``crs``
        and ``crs_wkt`` keyword arguments are passed, the latter will
        trump the former.

        In 'w' mode, kwargs will be mapped to OGR layer creation
        options.
        """

        if not isinstance(path, string_types):
            raise TypeError("invalid path: %r" % path)
        if not isinstance(mode, string_types) or mode not in ('r', 'w', 'a'):
            raise TypeError("invalid mode: %r" % mode)
        if driver and not isinstance(driver, string_types):
            raise TypeError("invalid driver: %r" % driver)
        if schema and not hasattr(schema, 'get'):
            raise TypeError("invalid schema: %r" % schema)
        if crs and not isinstance(crs, compat.DICT_TYPES + string_types):
            raise TypeError("invalid crs: %r" % crs)
        if crs_wkt and not isinstance(crs_wkt, string_types):
            raise TypeError("invalid crs_wkt: %r" % crs_wkt)
        if encoding and not isinstance(encoding, string_types):
            raise TypeError("invalid encoding: %r" % encoding)
        if layer and not isinstance(layer, tuple(list(string_types) + [int])):
            raise TypeError("invalid name: %r" % layer)
        if vsi:
            if not isinstance(vsi, string_types) or not vfs.valid_vsi(vsi):
                raise TypeError("invalid vsi: %r" % vsi)
        if archive and not isinstance(archive, string_types):
            raise TypeError("invalid archive: %r" % archive)

        # Check GDAL version against drivers
        if (driver == "GPKG" and
                get_gdal_version_num() < calc_gdal_version_num(1, 11, 0)):
            raise DriverError(
                "GPKG driver requires GDAL 1.11.0, fiona was compiled "
                "against: {}".format(get_gdal_release_name()))

        self.session = None
        self.iterator = None
        self._len = 0
        self._bounds = None
        self._driver = None
        self._schema = None
        self._crs = None
        self._crs_wkt = None
        self.env = None
        self.enabled_drivers = enabled_drivers

        self.path = vfs.vsi_path(path, vsi, archive)

        if mode == 'w':
            if layer and not isinstance(layer, string_types):
                raise ValueError("in 'w' mode, layer names must be strings")
            if driver == 'GeoJSON':
                if layer is not None:
                    raise ValueError("the GeoJSON format does not have layers")
                self.name = 'OgrGeoJSON'
            # TODO: raise ValueError as above for other single-layer formats.
            else:
                self.name = layer or os.path.basename(os.path.splitext(path)[0])
        else:
            if layer in (0, None):
                self.name = 0
            else:
                self.name = layer or os.path.basename(os.path.splitext(path)[0])

        self.mode = mode

        if self.mode == 'w':
            if driver == 'Shapefile':
                driver = 'ESRI Shapefile'
            if not driver:
                raise DriverError("no driver")
            elif driver not in supported_drivers:
                raise DriverError(
                    "unsupported driver: %r" % driver)
            elif self.mode not in supported_drivers[driver]:
                raise DriverError(
                    "unsupported mode: %r" % self.mode)
            self._driver = driver

            if not schema:
                raise SchemaError("no schema")
            elif 'properties' not in schema:
                raise SchemaError("schema lacks: properties")
            elif 'geometry' not in schema:
                raise SchemaError("schema lacks: geometry")
            self._schema = schema

            if crs_wkt:
                self._crs_wkt = crs_wkt
            elif crs:
                if 'init' in crs or 'proj' in crs or 'epsg' in crs.lower():
                    self._crs = crs
                else:
                    raise CRSError("crs lacks init or proj parameter")

        if driver_count == 0:
            # create a local manager and enter
            self.env = AWSGDALEnv()
        else:
            self.env = AWSGDALEnv()
        self.env.__enter__()

        self._driver = driver
        kwargs.update(encoding=encoding or '')
        self.encoding = encoding

        try:
            if self.mode == 'r':
                self.session = Session()
                self.session.start(self, **kwargs)
            elif self.mode in ('a', 'w'):
                self.session = WritingSession()
                self.session.start(self, **kwargs)
        except IOError:
            self.session = None
            raise

        if self.session is not None:
            self.guard_driver_mode()
            if not self.encoding:
                self.encoding = self.session.get_fileencoding().lower()

    def __repr__(self):
        return "<%s Collection '%s', mode '%s' at %s>" % (
            self.closed and "closed" or "open",
            self.path + ":" + str(self.name),
            self.mode,
            hex(id(self)))

    def guard_driver_mode(self):
        driver = self.session.get_driver()
        if driver not in supported_drivers:
            raise DriverError("unsupported driver: %r" % driver)
        if self.mode not in supported_drivers[driver]:
            raise DriverError("unsupported mode: %r" % self.mode)

    @property
    def driver(self):
        """Returns the name of the proper OGR driver."""
        if not self._driver and self.mode in ("a", "r") and self.session:
            self._driver = self.session.get_driver()
        return self._driver

    @property
    def schema(self):
        """Returns a mapping describing the data schema.

        The mapping has 'geometry' and 'properties' items. The former is a
        string such as 'Point' and the latter is an ordered mapping that
        follows the order of fields in the data file.
        """
        if not self._schema and self.mode in ("a", "r") and self.session:
            self._schema = self.session.get_schema()
        return self._schema

    @property
    def crs(self):
        """Returns a Proj4 string."""
        if self._crs is None and self.session:
            self._crs = self.session.get_crs()
        return self._crs

    @property
    def crs_wkt(self):
        """Returns a WKT string."""
        if self._crs_wkt is None and self.session:
            self._crs_wkt = self.session.get_crs_wkt()
        return self._crs_wkt

    @property
    def meta(self):
        """Returns a mapping with the driver, schema, crs, and additional
        properties."""
        return {
            'driver': self.driver, 'schema': self.schema, 'crs': self.crs,
            'crs_wkt': self.crs_wkt}

    profile = meta

    def filter(self, *args, **kwds):
        """Returns an iterator over records, but filtered by a test for
        spatial intersection with the provided ``bbox``, a (minx, miny,
        maxx, maxy) tuple or a geometry ``mask``.

        Positional arguments ``stop`` or ``start, stop[, step]`` allows
        iteration to skip over items or stop at a specific item.
        """
        if self.closed:
            raise ValueError("I/O operation on closed collection")
        elif self.mode != 'r':
            raise IOError("collection not open for reading")
        if args:
            s = slice(*args)
            start = s.start
            stop = s.stop
            step = s.step
        else:
            start = stop = step = None
        bbox = kwds.get('bbox')
        mask = kwds.get('mask')
        if bbox and mask:
            raise ValueError("mask and bbox can not be set together")
        self.iterator = Iterator(
            self, start, stop, step, bbox, mask)
        return self.iterator

    def items(self, *args, **kwds):
        """Returns an iterator over FID, record pairs, optionally
        filtered by a test for spatial intersection with the provided
        ``bbox``, a (minx, miny, maxx, maxy) tuple or a geometry
        ``mask``.

        Positional arguments ``stop`` or ``start, stop[, step]`` allows
        iteration to skip over items or stop at a specific item.
        """
        if self.closed:
            raise ValueError("I/O operation on closed collection")
        elif self.mode != 'r':
            raise IOError("collection not open for reading")
        if args:
            s = slice(*args)
            start = s.start
            stop = s.stop
            step = s.step
        else:
            start = stop = step = None
        bbox = kwds.get('bbox')
        mask = kwds.get('mask')
        if bbox and mask:
            raise ValueError("mask and bbox can not be set together")
        self.iterator = ItemsIterator(
            self, start, stop, step, bbox, mask)
        return self.iterator

    def keys(self, *args, **kwds):
        """Returns an iterator over FIDs, optionally
        filtered by a test for spatial intersection with the provided
        ``bbox``, a (minx, miny, maxx, maxy) tuple or a geometry
        ``mask``.

        Positional arguments ``stop`` or ``start, stop[, step]`` allows
        iteration to skip over items or stop at a specific item.
        """
        if self.closed:
            raise ValueError("I/O operation on closed collection")
        elif self.mode != 'r':
            raise IOError("collection not open for reading")
        if args:
            s = slice(*args)
            start = s.start
            stop = s.stop
            step = s.step
        else:
            start = stop = step = None
        bbox = kwds.get('bbox')
        mask = kwds.get('mask')
        if bbox and mask:
            raise ValueError("mask and bbox can not be set together")
        self.iterator = KeysIterator(
            self, start, stop, step, bbox, mask)
        return self.iterator

    def __contains__(self, fid):
        return self.session.has_feature(fid)

    values = filter

    def __iter__(self):
        """Returns an iterator over records."""
        return self.filter()

    def __next__(self):
        """Returns next record from iterator."""
        warnings.warn("Collection.__next__() is buggy and will be removed in "
                      "Fiona 2.0. Switch to `next(iter(collection))`.",
                      DeprecationWarning, stacklevel=2)
        if not self.iterator:
            iter(self)
        return next(self.iterator)

    next = __next__

    def __getitem__(self, item):
        return self.session.__getitem__(item)

    def writerecords(self, records):
        """Stages multiple records for writing to disk."""
        if self.closed:
            raise ValueError("I/O operation on closed collection")
        if self.mode not in ('a', 'w'):
            raise IOError("collection not open for writing")
        self.session.writerecs(records, self)
        self._len = self.session.get_length()
        self._bounds = self.session.get_extent()

    def write(self, record):
        """Stages a record for writing to disk."""
        self.writerecords([record])

    def validate_record(self, record):
        """Compares the record to the collection's schema.

        Returns ``True`` if the record matches, else ``False``.
        """
        # Currently we only compare keys of properties, not the types of
        # values.
        return (
            set(record['properties'].keys()) ==
            set(self.schema['properties'].keys()) and
            self.validate_record_geometry(record))

    def validate_record_geometry(self, record):
        """Compares the record's geometry to the collection's schema.

        Returns ``True`` if the record matches, else ``False``.
        """
        # Shapefiles welcome mixes of line/multis and polygon/multis.
        # OGR reports these mixed files as type "Polygon" or "LineString"
        # but will return either these or their multi counterparts when
        # reading features.
        if (self.driver == "ESRI Shapefile" and
                "Point" not in record['geometry']['type']):
            return record['geometry']['type'].lstrip(
                "Multi") == self.schema['geometry'].lstrip("3D ").lstrip(
                    "Multi")
        else:
            return (
                record['geometry']['type'] ==
                self.schema['geometry'].lstrip("3D "))

    def __len__(self):
        if self._len <= 0 and self.session is not None:
            self._len = self.session.get_length()
        if self._len < 0:
            # Raise TypeError when we don't know the length so that Python
            # will treat Collection as a generator
            raise TypeError("Layer does not support counting")
        return self._len

    @property
    def bounds(self):
        """Returns (minx, miny, maxx, maxy)."""
        if self._bounds is None and self.session is not None:
            self._bounds = self.session.get_extent()
        return self._bounds

    def flush(self):
        """Flush the buffer."""
        if self.session is not None:
            self.session.sync(self)
            new_len = self.session.get_length()
            self._len = new_len > self._len and new_len or self._len
            self._bounds = self.session.get_extent()

    def close(self):
        """In append or write mode, flushes data to disk, then ends
        access."""
        if self.session is not None:
            if self.mode in ('a', 'w'):
                self.flush()
            log.debug("Flushed buffer")
            self.session.stop()
            log.debug("Stopped session")
            self.session = None
            self.iterator = None
        if self.env:
            self.env.__exit__()

    @property
    def closed(self):
        """``False`` if data can be accessed, otherwise ``True``."""
        return self.session is None

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()

    def __del__(self):
        # Note: you can't count on this being called. Call close() explicitly
        # or use the context manager protocol ("with").
        self.close()


def get_filetype(bytesbuf):
    """Detect compression type of bytesbuf.

    ZIP only. TODO: add others relevant to GDAL/OGR."""
    if bytesbuf[:4].startswith(b'PK\x03\x04'):
        return 'zip'
    else:
        return ''


class BytesCollection(Collection):
    """BytesCollection takes a buffer of bytes and maps that to
    a virtual file that can then be opened by fiona.
    """
    def __init__(self, bytesbuf, **kwds):
        """Takes buffer of bytes whose contents is something we'd like
        to open with Fiona and maps it to a virtual file.
        """
        if not isinstance(bytesbuf, binary_type):
            raise ValueError("input buffer must be bytes")

        # Hold a reference to the buffer, as bad things will happen if
        # it is garbage collected while in use.
        self.bytesbuf = bytesbuf

        # Map the buffer to a file. If the buffer contains a zipfile
        # we take extra steps in naming the buffer and in opening
        # it. If the requested driver is for GeoJSON, we append an an
        # appropriate extension to ensure the driver reads it.
        filetype = get_filetype(self.bytesbuf)
        ext = ''
        if filetype == 'zip':
            ext = '.zip'
        elif kwds.get('driver') == "GeoJSON":
            ext = '.json'
        self.virtual_file = buffer_to_virtual_file(self.bytesbuf, ext=ext)

        # Instantiate the parent class.
        super(BytesCollection, self).__init__(self.virtual_file, vsi=filetype,
                                              encoding='utf-8', **kwds)

    def close(self):
        """Removes the virtual file associated with the class."""
        super(BytesCollection, self).close()
        if self.virtual_file:
            remove_virtual_file(self.virtual_file)
            self.virtual_file = None
            self.bytesbuf = None

    def __repr__(self):
        return "<%s BytesCollection '%s', mode '%s' at %s>" % (
            self.closed and "closed" or "open",
            self.path + ":" + str(self.name),
            self.mode,
            hex(id(self)))
