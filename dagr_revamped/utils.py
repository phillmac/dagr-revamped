import json
import logging
from collections.abc import Iterable, Mapping
from io import StringIO
from pathlib import Path, PurePosixPath
from pprint import pformat, pprint
from random import choice

from mechanicalsoup import StatefulBrowser
from requests import adapters as req_adapters
from requests import session as req_session

logger = logging.getLogger(__name__)


def make_dirs(directory):
    logger = logging.getLogger(__name__)
    if not isinstance(directory, Path):
        directory = Path(directory).resolve()
    if not directory.exists():
        directory.mkdir(parents=True)
        logger.debug('Created dir {}'.format(directory))


def strip_topdirs(config, directory):
    if not isinstance(directory, Path):
        directory = Path(directory).resolve()

    index = len(config.output_dir.parts)
    dirparts = directory.parts[index:]
    return Path(*dirparts)


def get_base_dir(config, mode, deviant=None, mval=None):
    logger = logging.getLogger(__name__)
    directory = config.output_dir
    if deviant:
        base_dir = directory.joinpath(deviant, mode)
    else:
        base_dir = Path(directory, mode)
    if mval:
        mval = Path(mval)
        use_old = config.get('dagr.subdirs', 'useoldformat')
        move = config.get('dagr.subdirs', 'move')
        old_path = base_dir.joinpath(mval)
        new_path = base_dir.joinpath(mval.name)
        if use_old:
            base_dir = old_path
            logger.debug('Old format subdirs enabled')
        elif not new_path == old_path and old_path.exists():
            if move:
                if new_path.exists():
                    logger.error('Unable to move {}: subfolder {} already exists'.format(
                        old_path, new_path))
                    return
                logger.log(level=25, msg='Moving {} to {}'.format(
                    old_path, new_path))
                try:
                    parent = old_path.parent
                    old_path.rename(new_path)
                    parent.rmdir()
                    base_dir = new_path
                except OSError:
                    logger.error('Unable to move subfolder {}:'.format(
                        new_path), exc_info=True)
                    return
            else:
                logger.debug('Move subdirs not enabled')
        else:
            base_dir = new_path
    base_dir = base_dir.resolve()
    logger.debug('Base dir: {}'.format(base_dir))
    try:
        make_dirs(base_dir)
    except OSError:
        logger.error('Unable to create base_dir', exc_info=True)
        return
    logger.log(level=5, msg=pformat(locals()))
    return base_dir


def buffered_file_write(json_content, fname):
    if not isinstance(fname, Path):
        fname = Path(fname)
    buffer = StringIO()
    json.dump(json_content, buffer, indent=4, sort_keys=True)
    buffer.seek(0)
    fname.write_text(buffer.read())


def update_d(d, u):
    for k, v in u.items():
        if isinstance(v,  Mapping):
            d[k] = update_d(d.get(k, {}), v)
        elif isinstance(d.get(k), Iterable):
            if isinstance(v, Iterable):
                d[k].extend(v)
            else:
                d[k].append(v)
        else:
            d[k] = v
    return d


def convert_queue(config, queue):
    logger = logging.getLogger(__name__)
    queue = {k.lower(): v for k, v in queue.items()}
    converted = queue.get('deviants', {})
    if None in converted:
        update_d(converted, {None: converted.pop(None)})
    for ndmode in config.get('deviantart', 'ndmodes').split(','):
        if ndmode in queue:
            mvals = queue.pop(ndmode)
            update_d(converted, {None: {ndmode: mvals}})
    for mode in config.get('deviantart', 'modes').split(','):
        data = queue.get(mode)
        if isinstance(data, Mapping):
            for k, v in data.items():
                update_d(converted, {k: {mode: v}})
        elif isinstance(data, Iterable):
            for v in data:
                update_d(converted, {v: {mode: None}})
        else:
            logger.debug('Mode {} not present'.format(mode))
    return converted


def load_bulk_files(files):
    logger = logging.getLogger(__name__)
    bulk_queue = {}
    files = [Path(fn).resolve() for fn in files]
    for fn in files:
        logger.debug('Loading file {}'.format(fn))
        update_d(bulk_queue, load_json(fn))
    return bulk_queue


def filter_deviants(dfilter, queue):
    if dfilter is None or not dfilter:
        return queue
    logger = logging.getLogger(__name__)
    logger.info('Deviant filter: {}'.format(pformat(dfilter)))
    results = dict((k, queue.get(k)) for k in queue.keys() if k in dfilter)
    logger.log(level=5, msg='Filter results: {}'.format(pformat(results)))
    return dict((k, queue.get(k)) for k in queue.keys() if k in dfilter)


def compare_size(dest, content):
    logger = logging.getLogger(__name__)
    if not isinstance(dest, Path):
        dest = Path(dest)
    if not dest.exists():
        return False
    current_size = dest.stat().st_size
    best_size = len(content)
    if not current_size < best_size:
        return True
    logger.info('Current file {} is smaller by {} bytes'.format(
        dest, best_size - current_size))
    return False


def create_browser(mature=False, user_agent=None):
    user_agents = (
        'Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/535.1'
        ' (KHTML, like Gecko) Chrome/14.0.835.202 Safari/535.1',
        'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:7.0.1) Gecko/20100101',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_6_8) AppleWebKit/534.50'
        ' (KHTML, like Gecko) Version/5.1 Safari/534.50',
        'Mozilla/4.0 (compatible; MSIE 8.0; Windows NT 6.1; Trident/4.0)',
        'Opera/9.99 (Windows NT 5.1; U; pl) Presto/9.9.9',
        'Mozilla/5.0 (Macintosh; U; Intel Mac OS X 10_5_6; en-US)'
        ' AppleWebKit/530.5 (KHTML, like Gecko) Chrome/ Safari/530.5',
        'Mozilla/5.0 (Windows; U; Windows NT 6.1; en-US) AppleWebKit/533.2'
        ' (KHTML, like Gecko) Chrome/6.0',
        'Mozilla/5.0 (Windows; U; Windows NT 6.1; pl; rv:1.9.1)'
        ' Gecko/20090624 Firefox/3.5 (.NET CLR 3.5.30729)'
    )
    session = req_session()
    session.headers.update({'Referer': 'https://www.deviantart.com/'})

    if mature:
        session.cookies.update({'agegate_state': '1'})
    session.mount('https://', req_adapters.HTTPAdapter(max_retries=3))

    if user_agent is None:
        user_agent = choice(user_agents)

    return StatefulBrowser(
        session=session,
        user_agent=user_agent)


def backup_cache_file(fpath):
    if not isinstance(fpath, Path):
        fpath = Path(fpath)
    fpath = fpath.resolve()
    backup = fpath.with_suffix('.bak')
    if fpath.exists():
        if backup.exists():
            backup.unlink()
        fpath.rename(backup)


def unlink_lockfile(lockfile):
    logger = logging.getLogger(__name__)
    if not isinstance(lockfile, Path):
        lockfile = Path(lockfile)
    if lockfile.exists():
        try:
            lockfile.unlink()
        except (PermissionError, OSError):
            logger.warning('Unable to unlock {}'.format(lockfile.parent))


def shorten_url(url):
    p = PurePosixPath()
    for u in Path(url).parts[2:]:
        p = p.joinpath(u)
    return str(p)


def artist_from_url(url):
    artist_url_p = PurePosixPath(url).parent.parent
    artist_name = artist_url_p.name
    shortname = PurePosixPath(url).name
    return (artist_url_p, artist_name, shortname)


def save_json(fpath, data, do_backup=True):
    if isinstance(data, set):
        data = list(data)
    p = fpath if isinstance(fpath, Path) else Path(fpath)
    p = p.resolve()
    if do_backup:
        backup_cache_file(p)
    buffered_file_write(data, p)
    logger.log(
        level=15, msg=f"Saved {len(data)} items to {fpath}")


def load_json(fpath):
    p = fpath.resolve() if isinstance(fpath, Path) else Path(fpath).resolve()
    buffer = StringIO(p.read_text())
    return json.load(buffer)


def load_primary_or_backup(fpath, use_backup=True, warn_not_found=True):
    if not isinstance(fpath, Path):
        fpath = Path(fpath)
    backup = fpath.with_suffix('.bak')
    try:
        if fpath.exists():
            return load_json(fpath)
        elif warn_not_found:
            logger.log(
                level=15, msg='Primary {} cache not found'.format(fpath.name))
    except json.JSONDecodeError:
        logger.warning(
            'Unable to decode primary {} cache:'.format(fpath.name), exc_info=True)
        fpath.replace(fpath.with_suffix('.bad'))
    except:
        logger.warning(
            'Unable to load primary {} cache:'.format(fpath.name), exc_info=True)
    try:
        if use_backup:
            if backup.exists():
                return load_json(backup)
        elif warn_not_found:
            logger.log(
                level=15, msg='Backup {} cache not found'.format(backup.name))
    except:
        logger.warning(
            'Unable to load backup {} cache:'.format(backup.name), exc_info=True)

