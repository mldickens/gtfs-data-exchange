import base64
import datetime
import glob
import hashlib
import os
import re
import sys
import random
import string
import subprocess
import time
import urllib
import urllib2
import tornado.options
import logging
import simplejson as json

import BeautifulSoup
import MultipartPostHandler
import S3

random.seed()

class StopNow(Exception):
    pass

class DownloadError(Exception):
    pass

class Crawler:
    def __init__(self):
        self.homebase = tornado.options.options.remote_endpoint
        logging.info('--remote-endpoint is %s' % self.homebase)
        self.opener = urllib2.build_opener(MultipartPostHandler.MultipartPostHandler)
        self.opener.addheaders = [('User-agent', 'Mozilla/5.0 (gtfs feed crawler http://www.gtfs-data-exchange.com/)')]
        urllib2.install_opener(self.opener)
        self.seenlinks = []
        self.totalsleep = 0.0
        self.start_time = time.time()
        self.totalurls = 0
        self.total304 = 0
        self.totaldownload = 0
        self.errorurls = 0
        self.exit = False
        self.connect_s3()
    
    def connect_s3(self):
        if tornado.options.options.shunt_s3:
            logging.info('skipping s3 connection --shunt-s3')
            return
        aws_access_key_id = _utf8(tornado.options.options.aws_key)
        aws_secret_access_key = _utf8(tornado.options.options.aws_secret)
        self.conn = S3.AWSAuthConnection(aws_access_key_id, aws_secret_access_key)

    def is_special_url(self,url):
        return {'http://www2.septa.org/developer':'septa'}.get(url,None)
    
    def get_next_crawl_url(self):
        if self.exit:
            raise StopNow()
        
        if tornado.options.options.crawl_url:
            url = tornado.options.options.crawl_url
            assert url.startswith("http://") or url.startswith("ftp://") or url.startswith("https://")
            self.exit = True
            settings= {'recurse':1, 'download_as':'gtfs-archiver', 'show_url':True, 'post_text':''}
            settings['url']=url
            return url, settings
        
        data = self.gtfs_data_exchange_urlopen(self.homebase + 'crawl/nexturl').read()
        if data == 'NONE' or not data.startswith('{'):
            raise StopNow()
        settings = json.loads(data)
        logging.debug('<= %r' % settings['url'])
        return settings['url'], settings
    
    def headers(self,url):
        if tornado.options.options.skip_304:
            return None
        url = self.homebase + 'crawl/headers?' + urllib.urlencode({'url': url})
        data = self.gtfs_data_exchange_urlopen(url).read()
        if not data or data == 'NONE':
            return None
        try:
            return json.loads(data)
        except:
            logging.error('failed loading headers %r' % data)
        
    def save_headers(self, url, headers):
        
        # parse out the content type;
        # TODO: move to a method that better describes this parsing
        if headers.get('Content-Disposition') and 'filename=' in headers.get('Content-Disposition'):
            filename = headers.get('Content-Disposition').split('filename=', 1)[-1].strip('"')
            if filename.endswith(".zip"):
                self.content_disposition = filename
        
        if not tornado.options.options.save_headers:
            logging.info('skipping save of headers %r for %r' % (headers, url))
            return
        
        if tornado.options.options.crawl_url:
            logging.info('skipping save of headers; manual crawl')
            return
        
        ## Last-Modified -> If-Modified-Since
        save_headers = {}
        if headers.get('Last-Modified', None):
            headers['If-Modified-Since'] = headers.get('Last-Modified', None)
        for k in ['If-Modified-Since','Etag']:
            if headers.get(k, None):
                save_headers[k]=headers[k]
        
        req = urllib2.Request(self.homebase+'crawl/headers')
        req.add_data({'url': url,'headers':json.dumps(save_headers)})
        self.gtfs_data_exchange_urlopen(req)
    
    def gtfs_data_exchange_urlopen(self, req):
        if isinstance(req, (str, unicode)):
            assert req.startswith("http")
            req = urllib2.Request(req)
        assert isinstance(req, urllib2.Request)
        assert isinstance(tornado.options.options.token, (str, unicode))
        req.add_header('X-Crawler-Token', tornado.options.options.token)
        return urllib2.urlopen(req)
    
    def craw_special(self,url,settings):
        agency = self.is_special_url(url)
        if agency != 'septa':
            return
        url = 'http://www2.septa.org/developer/download.php?fc=septa_gtfs.zip&download=download'
        localdir = '/tmp/gtfs/ftpmirror/%s' % agency
        logging.debug('crawling %r' % url)
        
        if not os.path.exists(localdir):
            os.makedirs(localdir)

        for filename in ('gtfs_data.zip','google_rail.zip','google_bus.zip'):
            if os.path.exists('%s/%s' % (localdir,filename)):
                logging.debug('removing old %s/%s' % (localdir, filename))
                os.unlink('%s/%s' % (localdir, filename))
        
        zipdata = self.getUrl(url,None)
        f = open('%s/gtfs_data.zip'%localdir,'wb')
        f.write(zipdata)
        f.close()
        self.totaldownload +=1
        
        cmd = 'unzip "%s/gtfs_data.zip" -d "%s"' % (localdir,localdir)
        pipe = subprocess.Popen(cmd, close_fds=True, stdout=subprocess.PIPE, shell=True)
        data = pipe.stdout.read()
        logging.debug(data[:1024])
        
        os.unlink('%s/gtfs_data.zip' % localdir)
        logging.debug('removing %s/gtfs_data.zip' % localdir)
        
        ## unzip
        self.crawlLocalDir(localdir,settings,"septa:")
            
    
    def crawl_ftp(self, url, settings):
        ## parse the url
        ## run ftpmirror.py
        ## get the zip files and check the md5's
        pattern = re.compile('ftp://((?P<username>.*):(?P<password>.*)@)?(?P<hostname>.+?)/(?P<path>.*)')
        m = pattern.match(url)
        if not m:
            logging.warning('** no ftp match %r' % url)
            return
        g = m.groupdict()
        logging.debug(g)
        username = g.get('username', 'anonymous')
        password = g.get('password', '')
        hostname = g['hostname']
        fullpath = g['path']
            
        if fullpath.endswith('.zip'):
            path = '/'.join(fullpath.split('/')[:-1])
        else:
            path = fullpath
        
        if not os.path.exists('/tmp/gtfs/ftpmirror/'):
            os.mkdir('/tmp/gtfs/ftpmirror/')
        localdir = "/tmp/gtfs/ftpmirror/%s" % hostname
        if not os.path.exists(localdir):
            os.mkdir(localdir)
        scriptpath = '/'.join(__file__.split('/')[:-1])
        command = 'python %s/ftpmirror.py -v -r -l "%s" -p "%s" %s "%s" "%s"' % (scriptpath, username, password, hostname, path, localdir)
        logging.info('>> running %s' % command)
        p = subprocess.Popen(command, stdout=subprocess.PIPE, close_fds=True, shell=True)
        data = p.stdout.read()
        logging.debug(data[:1024])
        del p
        logging.debug('>> done ftpmirror.py')
        self.crawlLocalDir(localdir, settings, 'ftp://%s' % hostname, localfile=None)
        
    def is_already_uploaded(self, md5sum):
        url = self.homebase + 'crawl/upload?' + urllib.urlencode({'md5sum': md5sum})
        data = self.gtfs_data_exchange_urlopen(url).read()
        already_uploaded = data.startswith("FOUND")
        logging.info('has %r been uploaded before? %r' % (md5sum, already_uploaded))
        return already_uploaded
    
    def get_next_upload_key(self):
        randstring = ''.join([random.choice(string.letters+string.digits) for x in range(20)])
        return 'queue/'+str(datetime.datetime.now())+'-'+randstring+'.zip'
        
    def crawlLocalDir(self, localdir, settings, desc, localfile=None):
        ## now find the files
        if localfile:
            files = glob.glob(localdir + '/' + localfile)
        else:
            files = glob.glob(localdir+'/*.zip') +  glob.glob(localdir+'/*/*.zip') + glob.glob(localdir+'/*/*/*.zip')
        for filename in files:
            shortname = filename.replace(localdir+'/','')
            
            file_contents = open(filename,'rb').read()
            md5sum = hashlib.md5(file_contents).hexdigest()
            if self.is_already_uploaded(md5sum):
                # note, we could save this file to our meta folder
                logging.info('already uploaded %s/%s' % (desc, shortname))
                continue
            
            if tornado.options.options.skip_uploads:
                logging.info('<fake> uploading %s/%s' % (desc, shortname))
                continue
            
            nextkey = self.get_next_upload_key()
            
            user = settings['download_as'] + '@gmail.com'
            comments = settings.get('post_text','') % {'filename':shortname}
            self.save_gtfs_file(file_contents, user, gtfs_crawler='t', comments=comments, filename=nextkey)
            logging.info('==> uploaded %s/%s as %s' % (desc, shortname, nextkey))
            
    def save_gtfs_file(self, contents, user, gtfs_crawler, comments, filename):
        assert '@' in user
        # if setting is prod
        if tornado.options.options.shunt_s3:
            if filename.startswith("queue/"):
                filename = filename[len("queue/"):]
            if not os.path.exists("/tmp/gtfs_s3/queue"):
                os.makedirs("/tmp/gtfs_s3/queue")
            filename = os.path.join("/tmp/gtfs_s3/queue", filename)
            logging.info('writing %s' % filename)
            f = open(filename, 'wb')
            f.write(contents)
            f.close()
            f = open(filename + '.meta', 'wb')
            f.write(json.dumps(dict(user=user, gtfs_crawler=gtfs_crawler, comments=comments)))
            f.close()
        else:
            obj = S3.S3Object(contents)
            obj.metadata['user'] = user
            obj.metadata['gtfs_crawler']=gtfs_crawler
            obj.metadata['comments'] = comments
            logging.info('putting %r' % filename)
            self.conn.put("gtfs", filename, obj)
        
    def should_skip_url(self,url):
        ## check the server to see if it's skipped there
        url = self.homebase + 'crawl/shouldSkip?' + urllib.urlencode({'url': url})
        result = self.gtfs_data_exchange_urlopen(url).read()
        if result.startswith('YES'):
            return True
    
    def getUrl(self, url, referer):
        self.content_disposition = None
        ## fetch previous request headers
        pattern = re.compile('http://(?P<username>.*):(?P<password>.*)@(?P<domain>.*?)/(?P<path>.*)')
        headers = self.headers(url) or {}
        m = pattern.match(url)
        if m:
            g = m.groupdict()
            raw = "%s:%s" % (g['username'], g['password'])
            logging.debug(raw)
            url = "http://%s/%s" % (g['domain'], g['path'])
            headers = self.headers(url) or {}

            if raw == "kcwww\\tr_pub_user:S6dffr$b":
                ## special case to use NTCL 
                headers['WWW-Authenticate'] = "NTLM TlRMTVNTUAACAAAACgAKADgAAAAFgokC0EdrnBZZhPUAAAAAAAAAAHgAeABCAAAABQLODgAAAA9LAEMAVwBXAFcAAgAKAEsAQwBXAFcAVwABABgAUABPADgAVwAxAEIASQBNAFQANgBYAFIABAAMAGsAYwAuAHcAdwB3AAMAJgBwAG8AOAB3ADEAYgBpAG0AdAA2AHgAcgAuAGsAYwAuAHcAdwB3AAUADABrAGMALgB3AHcAdwAAAAAA"
                logging.debug(headers['WWW-Authenticate'])
            else:
                headers['Authorization'] = 'Basic %s' % base64.b64encode(raw).strip()
                logging.debug(headers['Authorization'])

        if referer:
            headers['Referer']=referer
        ## get url
        self.totalurls +=1
        req = urllib2.Request(url)
        for h,v in headers.items():
            req.add_header(h, v)

        try:
            r = urllib2.urlopen(req)
            i = r.info()
            d = r.read()
        except urllib2.HTTPError, e:
            if e.code ==304:
                logging.info('got 304 on %r' % url)
                self.total304 +=1
                self.save_headers(url, e.headers)
                raise DownloadError()
            elif e.code == 110:
                logging.warning('connection refused on %r' % url)
                self.errorurls+=1
                raise DownloadError()
            else:
                logging.warning('got error %d on %r' % (e.code, url))
                self.errorurls+=1
                raise DownloadError()
        except urllib2.URLError, e:
            logging.error('got url error %r' % e)
            self.errorurls +=1
            raise DownloadError()
        
        self.save_headers(url, i)
        return d
    
    def crawl(self, url, settings, referer=None):
        if url in self.seenlinks:
            return
        self.seenlinks.append(url)
        if url.startswith('ftp://'):
            return self.crawl_ftp(url,settings)
        if self.is_special_url(url):
            return self.craw_special(url,settings)

        logging.debug('-+ %r recurse: %r' % (url, settings['recurse']))
        
        ## check for server side skipped urls
        if self.should_skip_url(url):
            logging.info('[skipped by server] %r' % url)
            return
        
        try:
            url_contents = self.getUrl(url, referer)
        except DownloadError:
            return
        except:
            logging.exception('** unknown exception on %r' % url)
            self.errorurls+=1
            return
            
        settings['recurse'] -=1

        if url.endswith(".pdf"):
            logging.info('pdf <skipped> %r' % url)
            return
        if url.endswith(".zip") or self.content_disposition:
            self.totaldownload +=1
            
            ## check if the md5 already exists
            md5sum = hashlib.md5(url_contents).hexdigest()
            if self.is_already_uploaded(md5sum):
                logging.info('already uploaded %r' % url)
                return
            
            nextkey = self.get_next_upload_key()
            
            if tornado.options.options.skip_uploads:
                logging.info('<fake> uploading as %s' % nextkey)
                return
            
            user = settings['download_as'] + '@gmail.com'
            if settings['show_url']:
                comments='Archived from %s' % url
            else:
                comments=''
            if settings['post_text']:
                comments += settings['post_text']
            self.save_gtfs_file(url_contents, user, gtfs_crawler='t', comments=comments, filename=nextkey)
            logging.info('uploaded %r as %r' % (url, nextkey))
            return

        ## find links
        try:
            soup = BeautifulSoup.BeautifulSoup(url_contents)
            for a in soup.findAll('a'):
                h = a['href']
                t = h

                # print "found link",a," href",h," in ",url
            
                if h.find('?') != -1:
                    #print "skipping because link has parameters",h
                    continue

                if h.startswith('/'):
                    ## absolute link w/o domain
                    b= url[:url.find('/',9)]
                    t = b + h
                elif not h.startswith('http'):
                    # it's a relative link
                    if url.count('/') >= 3 and not url.endswith('/'):
                        b = '/'.join(url.split('/')[:-1]) + '/'
                    else:
                        b = url ## http://...com/
                    t = b + h
                
                if not t.startswith(settings['url']):
                    # print "skipping",t,"does not start with",base
                    continue
            
                if '_vti_cnf' in t:
                    continue
            
                # print "link is now",t
                # print "sleeping"
                sleeptime = random.random()*3
                self.totalsleep += sleeptime
                time.sleep(sleeptime)
                if h.endswith('.zip'):
                    self.crawl(t, dict(settings), referer=url)
                elif settings['recurse'] >= 0:
                    self.crawl(t, dict(settings), referer=url)
        except:
            self.errorurls+=1
            logging.exception('error parsing page %r' % url)
    
    def run(self):
        while True:
            try:
                url, settings = self.get_next_crawl_url()
                self.crawl(url, settings)
            except StopNow:
                logging.info("got stop command")
                break
            except:
                logging.exception('an unknown error happened')
                break
            self.totalsleep += 1
            time.sleep(1)
        logging.info('Total Time for %0.2f seconds %0.2f of which were sleeping' % (time.time()-self.start_time,self.totalsleep))
        logging.info('%d urls (%d 304) (%d downloaded) (%d errors)' % (self.totalurls, self.total304,self.totaldownload,self.errorurls))

    def undo_last_run(self):
        logging.info('undoing last run')
        req = urllib2.Request(self.homebase+'crawl/undoLastRun')
        req.add_data({'_':''})
        data = self.gtfs_data_exchange_urlopen(req).read()
        logging.debug(data)


def _utf8(s):
    if isinstance(s, unicode):
        s = s.encode('utf-8')
    assert isinstance(s, str)
    return s

def main():
    tornado.options.define('save_headers', default=True, type=bool, help="save headers for a future run")
    tornado.options.define('skip_304', default=False, type=bool, help="skip 304's to force a re-crawl of everything")
    tornado.options.define('shunt_s3', type=bool, default=False, help="use a file-based s3 to pick remote endpoints")
    tornado.options.define('crawl_url', type=str, help="a specific URL to crawl (note default settings will apply)")
    tornado.options.define('skip_uploads', type=bool, default=False, help="skip uploading files")
    tornado.options.define('remote_endpoint', type=str, default="http://www.gtfs-data-exchange.com/", help="base url to gtfs-data-exchange. (in dev use http://localhost:8085/)")
    tornado.options.define('token', type=str, help="crawler access token")
    # 'http://6.gtfs-data-exchange.appspot.com/'
    tornado.options.define('aws_key', type=str)
    tornado.options.define('aws_secret', type=str)
    tornado.options.parse_command_line()
    
    if not tornado.options.options.token:
        sys.stderr.write("you must specify --token\n")
        sys.exit(1)
    
    c = Crawler()
    c.run()

if __name__ == "__main__":
    main()