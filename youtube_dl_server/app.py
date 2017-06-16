import functools
import logging
import traceback
import sys

from flask import Flask, Blueprint, current_app, jsonify, request, redirect, abort
import youtube_dl, urlparse, urllib, os, tempfile, shutil, boto3, time

from youtube_dl.version import __version__ as youtube_dl_version
from moviepy.video.io.ffmpeg_tools import ffmpeg_extract_subclip
from moviepy.editor import *
from contextlib import contextmanager
from boto3.s3.transfer import S3Transfer

from .version import __version__

if not hasattr(sys.stderr, 'isatty'):
    # In GAE it's not defined and we must monkeypatch
    sys.stderr.isatty = lambda: False


class SimpleYDL(youtube_dl.YoutubeDL):
    def __init__(self, *args, **kargs):
        super(SimpleYDL, self).__init__(*args, **kargs)
        self.add_default_info_extractors()

@contextmanager
def make_temp_directory():
    tempfile.tempdir = '/tmp'
    temp_dir = tempfile.mkdtemp()

    try:
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir)


def get_videos(url, extra_params):
    '''
    Get a list with a dict for every video founded
    '''
    ydl_params = {
        'format': 'best',
        'cachedir': False,
        'logger': current_app.logger.getChild('youtube-dl'),
    }
    ydl_params.update(extra_params)
    ydl = SimpleYDL(ydl_params)
    res = ydl.extract_info(url, download=False)
    return res


def flatten_result(result):
    r_type = result.get('_type', 'video')
    if r_type == 'video':
        videos = [result]
    elif r_type == 'playlist':
        videos = []
        for entry in result['entries']:
            videos.extend(flatten_result(entry))
    elif r_type == 'compat_list':
        videos = []
        for r in result['entries']:
            videos.extend(flatten_result(r))
    return videos


api = Blueprint('api', __name__)
client = boto3.client('s3',
    'us-east-1',
    aws_access_key_id='AKIAIOKFVXDG2HH2DQTQ',
    aws_secret_access_key='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'
)


def route_api(subpath, *args, **kargs):
    return api.route('/api/' + subpath, *args, **kargs)


def set_access_control(f):
    @functools.wraps(f)
    def wrapper(*args, **kargs):
        response = f(*args, **kargs)
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    return wrapper


@api.errorhandler(youtube_dl.utils.DownloadError)
@api.errorhandler(youtube_dl.utils.ExtractorError)
def handle_youtube_dl_error(error):
    logging.error(traceback.format_exc())
    result = jsonify({'error': str(error)})
    result.status_code = 500
    return result


class WrongParameterTypeError(ValueError):
    def __init__(self, value, type, parameter):
        message = '"{}" expects a {}, got "{}"'.format(parameter, type, value)
        super(WrongParameterTypeError, self).__init__(message)


@api.errorhandler(WrongParameterTypeError)
def handle_wrong_parameter(error):
    logging.error(traceback.format_exc())
    result = jsonify({'error': str(error)})
    result.status_code = 400
    return result


@api.before_request
def block_on_user_agent():
    user_agent = request.user_agent.string
    forbidden_uas = current_app.config.get('FORBIDDEN_USER_AGENTS', [])
    if user_agent in forbidden_uas:
        abort(429)


def query_bool(value, name, default=None):
    if value is None:
        return default
    value = value.lower()
    if value == 'true':
        return True
    elif value == 'false':
        return False
    else:
        raise WrongParameterTypeError(value, 'bool', name)


ALLOWED_EXTRA_PARAMS = {
    'format': str,
    'playliststart': int,
    'playlistend': int,
    'playlist_items': str,
    'playlistreverse': bool,
    'matchtitle': str,
    'rejecttitle': str,
    'writesubtitles': bool,
    'writeautomaticsub': bool,
    'allsubtitles': bool,
    'subtitlesformat': str,
    'subtitleslangs': list,
}


def get_result():
    url = request.args['url']
    extra_params = {}
    for k, v in request.args.items():
        if k in ALLOWED_EXTRA_PARAMS:
            convertf = ALLOWED_EXTRA_PARAMS[k]
            if convertf == bool:
                convertf = lambda x: query_bool(x, k)
            elif convertf == list:
                convertf = lambda x: x.split(',')
            extra_params[k] = convertf(v)
    return get_videos(url, extra_params)

def get_url_ext(url):
    path = urlparse.urlparse(url).path
    ext = os.path.splitext(path)[1]

    return ext

def filter_formats(result):
    formats = result['formats']
    formats = list(filter(lambda obj: obj['ext'] == 'mp4', formats))

    if result['extractor'] == 'vimeo':
        formats = list(filter(lambda obj: 'fragments' not in obj, formats))
        formats = list(filter(lambda obj: get_url_ext(obj['url']) == '.mp4', formats))

    result['formats'] = formats
    return result


@route_api('info')
@set_access_control
def info():
    url = request.args['url']
    result = get_result()
    result = filter_formats(result)
    key = 'info'
    if query_bool(request.args.get('flatten'), 'flatten', False):
        result = flatten_result(result)
        key = 'videos'
    result = {
        'url': url,
        key: result,
    }
    return jsonify(result)

@route_api('trim')
@set_access_control
def trim():
    """ Python/werkzerg encodes routing reading %2F as '/'
        we honestly do not want this behavior as it
        breaks vimeo video files url.

        well, we could obviously custom converters to
        override this behavior but that would be overkill
    """
    url = request.query_string.split('url=', 1)[1]
    clip = (VideoFileClip(url).subclip((0,00.00),(0,16.00)))

    with make_temp_directory() as temp_dir:
        files = ["%s/use_your_head.mp4" % temp_dir, "%s/use_your_head.gif" % temp_dir]
        prefix = "%r_output" % int(time.time())
        dir_code = temp_dir.split('/')[2]
        transfer = S3Transfer(client)

        clip.write_videofile(files[0], audio=False)
        clip.resize(0.3).write_gif(files[1])

        for file in files:
            arr = file.split('.')
            ext = arr[len(arr) - 1]

            transfer.upload_file(
                file, 'gifly.org', dir_code + '/' + prefix + '.' + ext
            )

    return jsonify({})

@route_api('play')
def play():
    result = flatten_result(get_result())
    return redirect(result[0]['url'])


@route_api('extractors')
@set_access_control
def list_extractors():
    ie_list = [{
        'name': ie.IE_NAME,
        'working': ie.working(),
    } for ie in youtube_dl.gen_extractors()]
    return jsonify(extractors=ie_list)


@route_api('version')
@set_access_control
def version():
    result = {
        'youtube-dl': youtube_dl_version,
        'youtube-dl-api-server': __version__,
    }
    return jsonify(result)

@route_api('test')
@set_access_control
def test_stuff():
    result = get_videos(request.args['url'], {})
    return jsonify(flatten_result(result))


app = Flask(__name__)
app.register_blueprint(api)
app.config.from_pyfile('../application.cfg', silent=True)
