# imagefap tools

## Gallery downloader

This is initial release.

It works for me.

In Linux, at least.

Example:

```
./fetch-gallery "https://www.imagefap.com/gallery.php?gid=5579075"
```

Configuration file should be in the same directory.
Proxies are not necessary but I did not test without them yet and probably never will.

Destination directory is not configurable yet, images are stored in a subdirectory
named as `id-name` in the current directory.

Dependencies:
* pycurl
* certifi

Implementation notes:
* it's fragile and may stop working if they make changes to markup and/or logic
* pieces of code are pulled from various projects so it's a hellish mix of synchronous and asynchronous code. Okay for now.
* curl is a state of art fetching tool, especially when compiled with BoringSSL.
  Although, `aiohttp` or even `requests` could be sufficient specifically for imagefap, but who knows.

Plans (depend on personal needs and/or your donations):
* folders and all user galleries fetching
* work via single Tor service with multiple circuits. Try using short circuits: 2-hops are possible,
  1-hop need investigations and probably custom client implementation.
* scrape full list of users and all their galleries for detailed analysis
