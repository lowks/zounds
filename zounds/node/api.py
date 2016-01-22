import os
import json
import tornado.ioloop
import tornado.web
import httplib
import numpy as np
from flow import Decoder
import traceback
from matplotlib import pyplot as plt
from io import BytesIO
import ast
import re
import uuid
import datetime
from timeseries import ConstantRateTimeSeriesFeature
from index import SearchResults
from duration import Seconds, Picoseconds
from timeseries import TimeSlice
from ogg_vorbis import OggVorbisFeature
from soundfile import SoundFile


class RangeUnitUnsupportedException(Exception):
    pass


class UnsatisfiableRangeRequestException(Exception):
    pass


class RangeRequest(object):
    def __init__(self, range_header):
        self.range_header = range_header
        self.re = re.compile(
                r'^(?P<unit>[^=]+)=(?P<start>[^-]+)-(?P<stop>.*?)$')

    def time_slice(self, start, stop):
        start = float(start)
        try:
            stop = float(stop)
        except ValueError:
            stop = None
        duration = \
            None if stop is None else Picoseconds(int(1e12 * (stop - start)))
        start = Picoseconds(int(1e12 * start))
        return TimeSlice(duration, start=start)

    def byte_slice(self, start, stop):
        start = int(start)
        try:
            stop = int(stop)
        except ValueError:
            stop = None
        return slice(start, stop)

    def range(self):
        raw = self.range_header
        if not raw:
            return slice(None)

        m = self.re.match(raw)
        if not m:
            return slice(None)

        units = m.groupdict()['unit']
        start = m.groupdict()['start']
        stop = m.groupdict()['stop']

        print units, start, stop

        if units == 'bytes':
            return self.byte_slice(start, stop)
        elif units == 'seconds':
            return self.time_slice(start, stop)
        else:
            raise RangeUnitUnsupportedException(units)


class NoMatchingSerializerException(Exception):
    pass


class TempResult(object):
    def __init__(self, data, content_type, is_partial=False):
        self.data = data
        self.content_type = content_type
        self.timestamp = datetime.datetime.utcnow()
        self.is_partial = is_partial


class RequestContext(object):
    def __init__(
            self,
            document=None,
            feature=None,
            slce=None,
            value=None):
        self.value = value
        self.slce = slce
        self.feature = feature
        self.document = document


# TODO: Seperate serializer for ConstantRateTimeSeries
# TODO: Seperate serializer for ogg vorbis with time slice
class DefaultSerializer(object):
    def __init__(self, content_type):
        super(DefaultSerializer, self).__init__()
        self._content_type = content_type

    def matches(self, context):
        if context.feature is None:
            return False
        return context.feature.encoder.content_type == self._content_type

    @property
    def content_type(self):
        return self._content_type

    def serialize(self, context):
        document = context.document
        feature = context.feature
        slce = context.slce
        flo = feature(_id=document._id, decoder=Decoder(), persistence=document)
        if slce.start:
            flo.seek(slce.start)
        if slce.stop:
            value = flo.read(slce.stop - slce.start)
        else:
            value = flo.read()
        return TempResult(value, self.content_type)


class OggVorbisSerializer(object):
    def __init__(self):
        super(OggVorbisSerializer, self).__init__()

    def matches(self, context):
        if context.feature is None:
            return False
        return \
            isinstance(context.feature, OggVorbisFeature) \
            and isinstance(context.slce, TimeSlice)

    @property
    def content_type(self):
        return 'audio/ogg'

    def serialize(self, context):
        feature = context.feature
        document = context.document
        slce = context.slce
        wrapper = feature(_id=document._id, persistence=document)
        samples = wrapper[slce]
        bio = BytesIO()
        with SoundFile(
                bio,
                mode='w',
                samplerate=wrapper.samplerate,
                channels=wrapper.channels,
                format='OGG', subtype='VORBIS') as sf:
            sf.write(samples)
        bio.seek(0)
        return TempResult(
                bio.read(), 'audio/ogg', is_partial=slce == TimeSlice())


def generate_image(data):
    fig = plt.figure()
    if len(data.shape) == 1:
        plt.plot(data)
    elif len(data.shape) == 2:
        mat = plt.matshow(np.rot90(data), cmap=plt.cm.gray)
        mat.axes.get_xaxis().set_visible(False)
        mat.axes.get_yaxis().set_visible(False)
    else:
        raise ValueError('cannot handle dimensions > 2')
    bio = BytesIO()
    plt.savefig(bio, bbox_inches='tight', pad_inches=0, format='png')
    bio.seek(0)
    fig.clf()
    plt.close()
    return TempResult(bio.read(), 'image/png')


class ConstantRateTimeSeriesSerializer(object):
    def __init__(self):
        super(ConstantRateTimeSeriesSerializer, self).__init__()

    def matches(self, context):
        return \
            isinstance(context.feature, ConstantRateTimeSeriesFeature) \
            and isinstance(context.slce, TimeSlice)

    @property
    def content_type(self):
        return 'image/png'

    def serialize(self, context):
        feature = context.feature
        document = context.document
        data = feature(_id=document._id, persistence=document)[context.slce]
        return generate_image(data)


class NumpySerializer(object):
    def __init__(self):
        super(NumpySerializer, self).__init__()

    def matches(self, context):
        if isinstance(context.feature, ConstantRateTimeSeriesFeature):
            return True

        return \
            isinstance(context.value, np.ndarray) \
            and len(context.value.shape) in (1, 2)

    @property
    def content_type(self):
        return 'image/png'

    def serialize(self, context):
        feature = context.feature
        document = context.document
        value = context.value
        if value is None:
            data = feature(_id=document._id, persistence=document)
        else:
            data = value
        return generate_image(data)


class SearchResultsSerializer(object):
    def __init__(self, visualization_feature, audio_feature):
        super(SearchResultsSerializer, self).__init__()
        self.visualization_feature = visualization_feature
        self.audio_feature = audio_feature

    def matches(self, context):
        return isinstance(context.value, SearchResults)

    @property
    def content_type(self):
        return 'application/vnd.zounds.searchresults+json'

    def _seconds(self, ts):
        return {
            'start': ts.start / Seconds(1),
            'duration': ts.duration / Seconds(1)
        }

    def _result(self, result):
        _id, ts = result
        template = '/zounds/{_id}/{feature}'
        return {
            'audio': template.format(_id=_id, feature=self.audio_feature.key),
            'visualization': template.format(
                    _id=_id, feature=self.visualization_feature.key),
            'slice': self._seconds(ts)
        }

    def serialize(self, context):
        output = {'results': map(self._result, context.value)}
        return TempResult(json.dumps(output), self.content_type)


class ZoundsApp(object):
    def __init__(
            self,
            base_path=r'/zounds/',
            model=None,
            visualization_feature=None,
            audio_feature=None,
            globals={},
            locals={}):

        super(ZoundsApp, self).__init__()
        self.locals = locals
        self.globals = globals
        self.model = model
        self.visualization_feature = visualization_feature
        self.audio_feature = audio_feature
        self.base_path = base_path
        self.serializers = [
            OggVorbisSerializer(),
            ConstantRateTimeSeriesSerializer(),
            DefaultSerializer('application/json'),
            DefaultSerializer('audio/ogg'),
            NumpySerializer(),
            SearchResultsSerializer(
                    self.visualization_feature,
                    self.audio_feature)
        ]
        self.temp = {}

        path, fn = os.path.split(__file__)
        with open(os.path.join(path, 'zounds.js')) as f:
            script = f.read()

        with open(os.path.join(path, 'index.html')) as f:
            self._html_content = f.read().replace(
                    '<script src="/zounds.js"></script>',
                    '<script>{}</script>'.format(script))

    def find_serializer(self, context):
        try:
            return filter(
                    lambda x: x.matches(context), self.serializers)[0]
        except IndexError:
            raise NoMatchingSerializerException()

    def serialize(self, context):
        serializer = self.find_serializer(context)
        return serializer.serialize(context)

    def temp_handler(self):

        app = self

        class TempHandler(tornado.web.RequestHandler):

            def get(self, _id):
                try:
                    result = app.temp[_id]
                except KeyError:
                    self.set_status(httplib.NOT_FOUND)
                    self.finish()
                    return
                self.set_header('Content-Type', result.content_type)
                self.set_header('Accept-Ranges', 'bytes')
                self.write(result.data)
                self.set_status(httplib.OK)
                self.finish()

        return TempHandler

    def repl_handler(self):
        document = self.model
        globals = self.globals
        locals = self.locals
        app = self

        class ReplHandler(tornado.web.RequestHandler):

            def _extract_feature(self, statement):
                root = ast.parse(statement)
                nodes = list(ast.walk(root))
                doc = None
                feature = None
                feature_name = None

                for node in nodes:
                    if doc and feature_name:
                        break

                    if isinstance(node, ast.Name) \
                            and node.id in locals \
                            and isinstance(locals[node.id], document):
                        doc = locals[node.id]
                        continue

                    if isinstance(node, ast.Attribute) \
                            and node.attr in document.features:
                        feature_name = node.attr
                        continue

                if feature_name:
                    feature = document.features[feature_name]

                return doc, feature

            def _add_url(self, statement, output, value):
                doc, feature = self._extract_feature(statement)
                try:
                    context = RequestContext(
                        document=doc,
                        feature=feature,
                        value=value,
                        slce=slice(None))
                    result = app.serialize(context)
                    temp_id = uuid.uuid4().hex
                    app.temp[temp_id] = result
                    output['url'] = '/zounds/temp/{temp_id}'.format(
                        temp_id=temp_id)
                    output['contentType'] = result.content_type
                except NoMatchingSerializerException:
                    pass

            def post(self):
                statement = self.request.body
                self.set_header('Content-Type', 'application/json')
                output = dict()

                try:
                    try:
                        value = eval(statement, globals, locals)
                        output['result'] = str(value)
                        self._add_url(statement, output, value)
                    except SyntaxError:
                        exec (statement, globals, locals)
                        output['result'] = ''
                    self.set_status(httplib.OK)
                except:
                    output['error'] = traceback.format_exc()
                    self.set_status(httplib.BAD_REQUEST)

                self.write(json.dumps(output))
                self.finish()

        return ReplHandler

    def feature_handler(self):

        document = self.model
        app = self

        class FeatureHandler(tornado.web.RequestHandler):

            def get(self, _id, feature):
                doc = document(_id)
                feature = document.features[feature]
                try:
                    slce = RangeRequest(self.request.headers['Range']).range()
                except KeyError:
                    slce = slice(None)
                context = RequestContext(
                        document=doc, feature=feature, slce=slce)
                try:
                    result = app.serialize(context)
                except UnsatisfiableRangeRequestException:
                    self.set_status(httplib.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.finish()
                self.set_header('Content-Type', result.content_type)
                self.set_header('Accept-Ranges', 'bytes')
                self.write(result.data)
                self.set_status(
                        httplib.PARTIAL_CONTENT if result.is_partial
                        else httplib.OK)
                self.finish()

        return FeatureHandler

    def ui_handler(self):
        app = self

        class UIHandler(tornado.web.RequestHandler):
            def get(self):
                self.set_header('Content-Type', 'text-html')
                self.write(app._html_content)
                self.set_status(httplib.OK)
                self.finish()

        return UIHandler

    def build_app(self):
        return tornado.web.Application([
            (r'/', self.ui_handler()),
            (r'/zounds/temp/(.+?)/?', self.temp_handler()),
            (r'/zounds/repl/?', self.repl_handler()),
            (r'/zounds/(.+?)/(.+?)/?', self.feature_handler())
        ])

    def start(self, port=8888):
        app = self.build_app()
        app.listen(port)
        tornado.ioloop.IOLoop.current().start()