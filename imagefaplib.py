import html
import json
import os # XXX use aiofiles
import re
import traceback
from urllib.parse import urljoin

import http


retry_count = 5  # XXX make configurable?

class _TryAnotherProxy(Exception):
    pass

class PageNotFound(Exception):
    pass


async def fetch_page(session, url, **kwargs):

    for _ in session.waysout:
        try:
            for _ in range(retry_count):
                response = await session.get(url, **kwargs)
                if response.status != '200':
                    raise _TryAnotherProxy()

                page_beginning = response.content[:512].lower()

                if b'it seems you are banned' in page_beginning:
                    raise _TryAnotherProxy()

                if b'404 not found' in page_beginning:
                    raise PageNotFound(f'Page not found: {url}')

                if b'<html' not in page_beginning:
                    # retry fetch partial page
                    continue

                if b'</html>' not in response.content[-256:].lower():
                    # retry fetch partial page
                    continue

                return response.content.decode('utf8'), response.real_url

        except PageNotFound:
            raise

        except _TryAnotherProxy:
            pass

        except http.ProxyError:
            pass

        except Exception as e:
            error = str(e)
            tb = traceback.format_exc()
            print('Failed', url, str(e))

        await session.next_proxy(wait=False)

    raise Exception(f'Unable to fetch {url}')


async def fetch_gallery(session, url, dest_dir='.'):

    print('Fetching page', url)
    gallery_page, gallery_url = await fetch_page(session, url)

    gallery_page, gallery_url = await ensure_one_page_view(session, gallery_url, gallery_page)

    images = collect_gallery_images(gallery_page, gallery_url)
    if len(images) == 0:
        raise Exception(f'Gallery seems to be empty {url}')

    gallery_info = extract_gallery_info(gallery_page, gallery_url)

    gallery_dir = os.path.join(dest_dir, f'{gallery_info["id"]}-{gallery_info["name"]}')
    os.makedirs(gallery_dir, exist_ok=True)

    # write gallery info
    with open(os.path.join(gallery_dir, 'info.json'), 'w', encoding='utf8') as f:
        json.dump(
            dict(
                gallery_info = gallery_info,
                images = images
            ),
            f,
            indent = 4,
            ensure_ascii = False
        )

    # gallery page does not contain direct links to full images,
    # "click" on the first image to get navi-cavi element which does contain a few,
    # fetch them, "click" on the next one after the last fetched image, and so on

    image_index = 0
    while image_index < len(images):
        image_page, image_page_url = await fetch_page(session, images[image_index]['page_url'], headers={'Referer': gallery_url})
        image_urls, total, idx = extract_navi_cavi(image_page, image_page_url)
        if idx != image_index:
            raise Exception(f'Image index {image_index} does not match extracted idx {idx}')
        for i, image_url in enumerate(image_urls):
            print('Fetching', image_url)
            image_filename = os.path.join(gallery_dir, images[i + idx]['filename'])
            await fetch_image(session, image_url, image_filename, headers={'Referer': image_page_url})
        image_index += len(image_urls)


_re_is_one_page = re.compile(r'<b>Detailed View</b>\s*</a>\s*&nbsp;\s*/\s*&nbsp;\s*<b>One Page</b>', re.I)
_re_one_page_link = re.compile(r'<b>Detailed View</b>\s*&nbsp;\s*/\s*&nbsp;\s*<a ([^>]+)>\s*<b>One Page</b>', re.I)
_re_href = re.compile('href=([\'"])(.*?)\\1')

async def ensure_one_page_view(session, gallery_url, gallery_page):
    '''
    Make sure gallery page is "one page" view.
    If not, fetch "one page" view.
    '''
    if _re_is_one_page.search(gallery_page):
        return gallery_page, gallery_url

    matchobj = _re_one_page_link.search(gallery_page)
    if not matchobj:
        raise Exception(f'Cannot extract "one page" link from {gallery_url}')
    matchobj = _re_href.search(matchobj.group(1))
    if not matchobj:
        raise Exception(f'Cannot extract "one page" link from {gallery_url}')
    href = html.unescape(matchobj.group(2))
    referrer = gallery_url
    url = urljoin(gallery_url, href)
    print('Fetching one page view', url)
    return await fetch_page(session, url, headers={'Referer': referrer})


_re_photo_link = re.compile('href=([\'"])(/photo/\\d+.*?)\\1', re.I)
_re_image_filename = re.compile('<font[^>]*><i>([^<]+)</i></font><BR>', re.I)
image_suffixes = ['.jpg', '.jpeg', '.gif']

def collect_gallery_images(gallery_page, gallery_url):
    '''
    Collect image file names and links to photo pages.
    '''
    images = []
    for photo_match in _re_photo_link.finditer(gallery_page):
        href = html.unescape(photo_match.group(2))
        page_url = urljoin(gallery_url, href)

        # file name follows photo link
        filename_match = _re_image_filename.search(gallery_page, photo_match.end(2))
        if filename_match is None:
            raise Exception(f'Cannot extract file name for "{page_url}" in {gallery_url}')

        image_filename = html.unescape(filename_match.group(1).strip())
        if not any(image_filename.endswith(suffix) for suffix in image_suffixes):
            raise Exception(f'Bad image filename {image_filename} in {gallery_url}')

        images.append(dict(
            filename = image_filename,
            page_url = page_url
        ))
    return images


_re_gallery_id = re.compile('<input type="hidden" id="gal_gid" value="(\d+)">', re.I)
_re_gallery_name = re.compile('Free porn pics of (.*?) 1 of \\d+ pic', re.I | re.DOTALL)
_re_gallery_description = re.compile('<span id="cnt_description">.*?<font[^>]*><span[^>]*>(.*?)</span>', re.I | re.DOTALL)
_re_user_name = re.compile('href=([\'"])https://www.imagefap.com/profile.php\\?user=(.*?)\\1', re.I)
_re_user_id = re.compile('href=([\'"])https://www.imagefap.com/blog.php\\?userid=(\\d+)\\1', re.I)

def extract_gallery_info(gallery_page, gallery_url):
    info = dict()
    matchobj = _re_gallery_id.search(gallery_page)
    if matchobj is None:
        raise Exception(f'Cannot extract gallery id from {gallery_url}')
    info['id'] = matchobj.group(1)

    matchobj = _re_gallery_name.search(gallery_page)
    if matchobj is None:
        raise Exception(f'Cannot extract gallery name from {gallery_url}')
    info['name'] = html.unescape(matchobj.group(1).strip())

    matchobj = _re_gallery_description.search(gallery_page)
    if matchobj is None:
        raise Exception(f'Cannot extract description from {gallery_url}')
    info['description'] = html.unescape(matchobj.group(1).strip())

    matchobj = _re_user_name.search(gallery_page)
    if matchobj is None:
        raise Exception(f'Cannot extract user name from {gallery_url}')
    info['username'] = html.unescape(matchobj.group(2))

    matchobj = _re_user_id.search(gallery_page)
    if matchobj is None:
        raise Exception(f'Cannot extract user id from {gallery_url}')
    info['userid'] = matchobj.group(2)

    return info


_re_image_url = re.compile('href=([\'"])(https://cdn.imagefap.com/images/full/.*?)\\1', re.I)
_re_navi_cavi = re.compile('<div id=([\'"])_navi_cavi\\1 [^>]+>', re.I)
_re_data_total = re.compile('data-total=([\'"])(\\d+)\\1', re.I)
_re_data_idx = re.compile('data-idx=([\'"])(\\d+)\\1', re.I)

def extract_navi_cavi(image_page, image_page_url):
    '''
    image navigation bar
    '''
    image_urls = [url for _, url in _re_image_url.findall(image_page)]
    matchobj =_re_navi_cavi.search(image_page)
    if matchobj is None:
        raise Exception(f'Cannot extract navi-cavi from {image_page_url}')
    navicavi = matchobj.group(0)
    total = int(_re_data_total.search(navicavi).group(2))
    idx = int(_re_data_idx.search(navicavi).group(2))
    return image_urls, total, idx


async def fetch_image(session, url, filename, **kwargs):

    for _ in session.waysout:
        try:
            for _ in range(retry_count):

                if os.path.exists(filename):
                    response = await session.head(url, **kwargs)
                    if response.status != '200':
                        raise _TryAnotherProxy()
                    response_headers = dict((k.lower(), v) for k, v in response.headers)
                    content_length = response_headers.get('content-length', None)
                    if content_length is not None and os.path.getsize(filename) == int(content_length):
                        print('Already downloaded', filename)
                        return

                response = await session.get(url, **kwargs)
                if response.status != '200':
                    raise _TryAnotherProxy()

                response_headers = dict((k.lower(), v) for k, v in response.headers)

                if not response_headers.get('content-type', '').startswith('image'):
                    raise _TryAnotherProxy()


                with open(filename, 'wb') as f:
                    f.write(response.content)
                print('Saved', filename)

                return

        except _TryAnotherProxy:
            pass

        except http.ProxyError:
            pass

        except Exception as e:
            error = str(e)
            tb = traceback.format_exc()
            print('Failed', url, str(e))

        await session.next_proxy(wait=False)

    raise Exception(f'Unable to fetch {url}')
