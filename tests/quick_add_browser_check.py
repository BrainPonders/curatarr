"""Direct result Add uses existing guarded endpoints without another details read."""
import argparse
from dataclasses import replace
from pathlib import Path
import socket
import tempfile
import threading
import time

from playwright.sync_api import sync_playwright,expect
import uvicorn
from curatarr.adapters import AdapterError
from curatarr.domain import DurableState,ArrRuntimeState,MediaIdentity
from curatarr.services.acquisition import MovieAcquisition
from curatarr.web.app import create_app
from curatarr.storage import Storage
from test_miniapp import MiniAppTests,signed_data
from test_movie_acquisition import Catalogue,Arr
from curatarr.services.catalogue_watch import CatalogueWatch
from test_catalogue_watch import Completion,Client
from test_direct_viewing import Native
from test_telegram_service import _movie


def main():
 parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
 fixture=MiniAppTests();fixture.setUp()
 config=replace(fixture.config,coordinator=replace(fixture.config.coordinator,enabled=True),
                users=replace(fixture.config.users,standard=(2,3,4)),
                radarr=replace(fixture.config.radarr,default_profile='Native profile'))
 cat,arr=Catalogue(),Arr()
 arr.options=lambda:{'profiles':[arr.profile,{'id':3,'name':'HD'},{'id':4,'name':'4K'},{'id':5,'name':'Remux'}],'roots':[arr.root]}
 with tempfile.TemporaryDirectory() as temp:
  app=create_app(config,database=Path(temp)/'state.sqlite',service=fixture.service,
                 acquisition_factory=lambda config,storage:MovieAcquisition(storage,cat,arr,CatalogueWatch(storage,cat,Native(cat),Completion(cat))))
  listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
  server=uvicorn.Server(uvicorn.Config(app,access_log=False,log_level='error'))
  thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
  try:
   deadline=time.monotonic()+10
   while not server.started:
    if time.monotonic()>deadline:raise RuntimeError('Fixture server failed')
    time.sleep(.02)
   with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='chromium')
    try:
     for user,(width,height) in enumerate([(320,640),(390,844),(844,390),(1280,900)],start=1):
      # Each viewport is an independent installation; retain uncertainty only
      # within that journey, never leak its reservation into the next fixture.
      storage=Storage(Path(temp)/'state.sqlite')
      with storage.connection:
       storage.connection.execute('DELETE FROM coordination_step')
       storage.connection.execute('DELETE FROM coordination_operation')
      storage.close()
      cat.value=None;cat.calls=[];cat.marked=False;arr.value=None;arr.calls=[];arr.lost=False;fixture.ryot.item=None;fixture.arr.item=None
      context=browser.new_context(viewport={'width':width,'height':height},color_scheme='dark' if width in {390,844} else 'light')
      page=context.new_page();errors=[];requests=[];page.on('pageerror',lambda error:errors.append(str(error)))
      page.on('request',lambda request:requests.append(request.url))
      page.route('https://telegram.org/js/*',lambda route:route.fulfill(body=''))
      page.route('https://image.tmdb.org/**',lambda route:route.fulfill(path=str((Path(__file__).resolve().parent / 'fixtures' / 'poster.png').resolve()),content_type='image/png'))
      page.add_init_script('window.Telegram={WebApp:{initData:'+repr(signed_data(user))+',safeAreaInset:{bottom:34,left:12,right:12},contentSafeAreaInset:{bottom:20,left:4,right:4},ready(){},expand(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};')
      page.goto(f'http://127.0.0.1:{port}');page.set_default_timeout(10000)
      def search():
       page.locator('#query').fill('Example');page.locator('#search-submit').click()
       expect(page.locator('#movie-title')).not_to_be_empty()
      def add():
       expect(page.locator('#result-add')).to_be_enabled();page.locator('#result-add').click()
       expect(page.locator('#acquisition-choices')).to_be_visible()
       expect(page.locator('#acquisition-title')).to_be_focused()
       expect(page.locator('#acquisition-mode-options')).to_be_visible()
       page.locator('#acquisition-mode-download').click()
       expect(page.locator('#acquisition-confirm')).to_be_enabled()
       expect(page.locator('#detail')).to_be_hidden()
      search();expect(page.locator('#result-add')).to_be_enabled()
      assert page.locator('#result-add').evaluate('(el)=>{const r=el.getBoundingClientRect();return r.top>=0 && r.bottom<=innerHeight && r.right<=innerWidth}')
      page.screenshot(path=str(args.output/f'result-{width}.png'))
      # Catalogue is reachable without fetching/opening download selections.
      page.locator('#result-add').click();expect(page.locator('#acquisition-title')).to_be_focused()
      expect(page.locator('#acquisition-confirm')).to_be_enabled()
      before_options=len([url for url in requests if url.endswith('/api/acquisition/options')])
      page.locator('#acquisition-mode-catalogue').click()
      expect(page.locator('#acquisition-choices')).to_be_hidden();expect(page.locator('#catalogue-watch-options')).to_be_visible()
      expect(page.locator('#catalogue-watch-details')).to_be_visible()
      expect(page.locator('#catalogue-watch-date')).to_be_disabled()
      expect(page.locator('#catalogue-watch-details')).to_have_css('opacity','0.5')
      page.locator('#catalogue-watched').check()
      expect(page.locator('#catalogue-watch-details')).to_be_visible();page.locator('#catalogue-watch-date').fill('2020-01-02')
      page.locator('#catalogue-rating-buttons button[data-rating="4"]').click()
      assert len([url for url in requests if url.endswith('/api/acquisition/options')])==before_options
      sheet=page.locator('#acquisition-dialog').bounding_box();footer=page.locator('#acquisition-dialog .acquisition-footer').bounding_box()
      date=page.locator('#catalogue-watch-date').bounding_box()
      body=page.locator('#acquisition-dialog .acquisition-body').bounding_box()
      assert date['x']>=body['x'] and date['x']+date['width']<=body['x']+body['width']
      page.locator('#acquisition-mode-download').click();expect(page.locator('#acquisition-profile')).to_have_value('2')
      other=page.locator('#acquisition-dialog').bounding_box();assert sheet==other,(sheet,other)
      assert page.locator('#acquisition-dialog .acquisition-footer').bounding_box()==footer
      page.locator('#acquisition-mode-catalogue').click()
      expect(page.locator('#catalogue-watched')).to_be_checked();expect(page.locator('#catalogue-watch-date')).to_have_value('2020-01-02')
      expect(page.locator('#catalogue-rating-buttons button[data-rating="4"]')).to_have_attribute('aria-pressed','true')
      page.locator('#catalogue-rating-buttons button[data-rating="4"]').click();expect(page.locator('#catalogue-watch-rating')).to_have_value('')
      page.locator('#catalogue-rating-buttons button[data-rating="4"]').click()
      page.locator('#acquisition-mode-catalogue').focus();page.keyboard.press('ArrowLeft')
      expect(page.locator('#acquisition-mode-download')).to_have_attribute('aria-selected','true')
      page.keyboard.press('ArrowRight');expect(page.locator('#acquisition-mode-catalogue')).to_have_attribute('aria-selected','true')
      page.screenshot(path=str(args.output/f'catalogue-watch-{width}.png'));page.locator('#acquisition-cancel').click()
      assert cat.calls==[] and arr.calls==[]

      # Download-option latency must never block the catalogue tab or erase its draft.
      held_options=[]
      page.route('**/api/acquisition/options',lambda route:held_options.append(route))
      page.locator('#result-add').click();page.locator('#acquisition-mode-catalogue').click()
      expect(page.locator('#acquisition-confirm')).to_be_enabled()
      page.locator('#catalogue-watched').check();page.locator('#catalogue-watch-date').fill('2019-02-03')
      page.locator('#catalogue-rating-buttons button[data-rating="3"]').click()
      page.locator('#acquisition-mode-download').click();expect(page.locator('#acquisition-confirm')).to_be_disabled()
      page.locator('#acquisition-mode-catalogue').click();expect(page.locator('#acquisition-confirm')).to_be_enabled()
      assert held_options
      held_options[0].continue_();page.unroute('**/api/acquisition/options')
      expect(page.locator('#acquisition-profile')).to_have_value('2')
      expect(page.locator('#catalogue-watch-date')).to_have_value('2019-02-03')
      expect(page.locator('#catalogue-rating-buttons button[data-rating="3"]')).to_have_attribute('aria-pressed','true')
      page.locator('#acquisition-cancel').click();assert cat.calls==[] and arr.calls==[]
      add();expect(page.locator('#acquisition-profile')).to_have_value('2');expect(page.locator('#acquisition-root')).to_have_value('1')
      expect(page.locator('#acquisition-profile-buttons button')).to_have_count(4)
      profiles=[button.bounding_box() for button in page.locator('#acquisition-profile-buttons button').all()]
      assert all(abs(box['x']-profiles[0]['x'])<1 and abs(box['width']-profiles[0]['width'])<1 for box in profiles)
      assert all(profiles[n]['y']+profiles[n]['height']<=profiles[n+1]['y'] for n in range(3))
      page.locator('#acquisition-profile-buttons button[data-profile="3"]').click();expect(page.locator('#acquisition-profile')).to_have_value('3')
      page.locator('#acquisition-mode-catalogue').click();page.locator('#acquisition-mode-download').click()
      expect(page.locator('#acquisition-profile')).to_have_value('3')
      page.locator('#acquisition-profile-buttons button[data-profile="2"]').click()
      expect(page.locator('#acquisition-title')).to_have_css('outline-style','none')
      page.screenshot(path=str(args.output/f'profile-buttons-{width}.png'))
      assert cat.calls==[] and arr.calls==[]
      page.locator('#acquisition-mode-catalogue').click();expect(page.locator('#acquisition-confirm')).to_be_enabled()
      expect(page.locator('#acquisition-choices')).to_be_hidden();page.locator('#acquisition-cancel').click()
      assert cat.calls==[] and arr.calls==[]
      add();expect(page.locator('#acquisition-confirm')).to_have_text('Confirm add and search')
      assert cat.calls==[] and arr.calls==[]
      page.locator('#acquisition-cancel').click();assert cat.calls==[] and arr.calls==[]
      add();page.locator('#acquisition-mode-catalogue').click();expect(page.locator('#acquisition-confirm')).to_be_enabled()
      page.locator('#acquisition-confirm').click();expect(page.locator('#acquisition-dialog')).not_to_be_visible()
      expect(page.locator('#result-acquisition-notice')).to_contain_text('Saved successfully',timeout=10000)
      expect(page.locator('#result-add')).to_be_disabled()
      assert arr.calls==[] and set(cat.value['collections'])=={'Library','Curatarr'}
      search();add();expect(page.locator('#acquisition-confirm')).to_have_text('Confirm add and search')
      assert arr.calls==[]
      page.screenshot(path=str(args.output/f'profile-{width}.png'))
      page.locator('#acquisition-confirm').click();expect(page.locator('#acquisition-dialog')).not_to_be_visible()
      expect(page.locator('#result-acquisition-notice')).to_contain_text('Saved successfully',timeout=10000)
      assert arr.calls==['add','search'];assert cat.calls.count('register')==1
      # Result Add entirely bypasses the expensive full Library status endpoint.
      assert not [url for url in requests if '/api/search/' in url and url.endswith('/details')],requests
      assert len([url for url in requests if '/api/search/' in url and url.endswith('/status')])==2,requests
      fixture.arr.item=_movie(runtime_state=ArrRuntimeState.ACTIVE_MISSING_MONITORED)
      search();expect(page.locator('#result-acquisition-notice')).to_have_text('Already active in Radarr.');expect(page.locator('#result-add')).to_be_disabled()
      fixture.arr.item=None;arr.value=None
      fixture.ryot.item=_movie(durable_states={DurableState.WATCHED,DurableState.ARCHIVED});cat.value['collections']['Archived']={}
      search();expect(page.locator('#result-add')).to_have_text('Reactivate movie');add()
      expect(page.locator('#acquisition-history')).to_contain_text('Already watched.')
      expect(page.locator('#acquisition-history')).to_contain_text('explicitly reactivates')
      expect(page.locator('#acquisition-confirm')).to_have_text('Confirm reactivation and search')
      page.screenshot(path=str(args.output/f'reactivate-{width}.png'));page.locator('#acquisition-cancel').click()
      fixture.ryot.item=_movie(identities={MediaIdentity('tmdb','999')})
      search();expect(page.locator('#result-acquisition-notice')).to_contain_text('identity checks');expect(page.locator('#result-add')).to_be_disabled()
      fixture.ryot.item=None
      original=fixture.arr.lookup_movie
      def fail(_):raise AdapterError('Unavailable service')
      fixture.arr.lookup_movie=fail
      search();expect(page.locator('#result-acquisition-notice')).to_contain_text('service and identity checks');expect(page.locator('#result-add')).to_be_disabled()
      fixture.arr.lookup_movie=original
      # The new catalogue route submits watched/date/rating in its one confirmation.
      cat.value=None;cat.calls=[];cat.client=Client(cat);arr.value=None;arr.calls=[]
      search();expect(page.locator('#result-add')).to_be_enabled();page.locator('#result-add').click()
      page.locator('#acquisition-mode-catalogue').click();page.locator('#catalogue-watched').check()
      page.locator('#catalogue-watch-date').fill('2020-01-02');page.locator('#catalogue-rating-buttons button[data-rating="4"]').click()
      page.locator('#acquisition-confirm').click();expect(page.locator('#acquisition-dialog')).not_to_be_visible()
      expect(page.locator('#result-acquisition-notice')).to_contain_text('Saved successfully',timeout=10000)
      assert arr.calls==[] and len(cat.value['history'])==1 and len(cat.value['reviews'])==1
      assert cat.value['history'][0]['reviewId']==cat.value['reviews'][0]['id']
      assert cat.value['history'][0]['finishedOn']=='2020-01-02T00:00:00+00:00'
      assert cat.value['reviews'][0]['rating']=='80'
      # Reloading after an uncertain service reply keeps the same saved request.
      cat.value=None;cat.calls=[];arr.value=None;arr.calls=[];arr.lost=True
      search();add();expect(page.locator('#acquisition-confirm')).to_have_text('Confirm add and search')
      page.locator('#acquisition-confirm').click()
      expect(page.locator('#result-acquisition-notice')).to_contain_text('service response is being checked',timeout=10000)
      search();expect(page.locator('#result-acquisition-notice')).to_contain_text('service response is being checked',timeout=10000)
      expect(page.locator('#result-add')).to_be_disabled();assert arr.calls==['add','search']
      assert not errors,errors
      assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
      context.close();print('Quick Add passed:',width,height,'two status reads / zero details reads for catalogue + download',flush=True)
    finally:browser.close()
  finally:server.should_exit=True;thread.join(timeout=10);fixture.doCleanups()


if __name__=='__main__':main()
