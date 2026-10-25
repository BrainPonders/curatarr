"""Real profile editor with synthetic providers; no native movie changes."""
import argparse
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import socket
import tempfile
import threading
import time

from playwright.sync_api import sync_playwright, expect
import uvicorn
from curatarr.adapters import AdapterResult
from curatarr.domain import ArrRuntimeState, DurableState, MediaIdentity
from curatarr.storage import Storage
from curatarr.web.app import create_app
from test_miniapp import MiniAppTests, signed_data
from test_radarr_profiles import Native, movie, service
from test_movie_acquisition import Catalogue
from test_telegram_service import _movie


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    fixture=MiniAppTests();fixture.setUp();native=Native();cat=Catalogue()
    config=replace(fixture.config,coordinator=replace(fixture.config.coordinator,enabled=True),users=replace(fixture.config.users,standard=(2,3,4)))
    fixture.ryot.item=_movie(durable_states={DurableState.LIBRARY})
    def arr_item(identity):
        return AdapterResult(replace(_movie(runtime_state=ArrRuntimeState.ACTIVE_DOWNLOADED_UNMONITORED),
            identities=frozenset({MediaIdentity('tmdb','558'),MediaIdentity('radarr','10')}),quality_profile_id=native.value['qualityProfileId'],movie_file_id=30))
    fixture.arr.lookup_movie=arr_item
    fixture.arr.quality_profile=lambda identifier:{'name':next(p['name'] for p in native.profiles if p['id']==identifier),'upgrade_allowed':False,'format_items':[]}
    fixture.arr.file_details=lambda file_id,movie_id:{'score':0,'quality':{'resolution':1080,'modifier':'none'},'format_ids':[],'language_ids':[1],'quality_cutoff_not_met':False}
    fixture.arr.core_formats=lambda:[]
    with tempfile.TemporaryDirectory() as temp:
        database=Path(temp)/'state.sqlite'
        app=create_app(config,database=database,service=fixture.service,profile_factory=lambda config,storage:service(storage,native,cat))
        listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,access_log=False,log_level='error'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
        try:
            deadline=time.monotonic()+10
            while not server.started:
                if time.monotonic()>deadline:raise RuntimeError('Fixture startup failed')
                time.sleep(.02)
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='chromium')
                try:
                    for user,(width,height) in enumerate([(320,640),(390,844),(844,390),(1280,900)],1):
                        storage=Storage(database)
                        with storage.connection:
                            storage.connection.execute('DELETE FROM coordination_step');storage.connection.execute('DELETE FROM coordination_operation')
                        storage.close()
                        native.value=movie();native.calls=[];native.lost=False;native.effect=True
                        cat.value=None;cat.marked=False;cat.register_library('558','Spider-Man 2','household','a'*32);cat.calls=[]
                        fixture.ryot.item=_movie(durable_states={DurableState.LIBRARY})
                        context=browser.new_context(viewport={'width':width,'height':height},color_scheme='dark' if width in {390,844} else 'light')
                        page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                        page.route('https://telegram.org/js/*',lambda route:route.fulfill(body=''))
                        page.route('https://image.tmdb.org/**',lambda route:route.fulfill(path=str((Path(__file__).resolve().parent / 'fixtures' / 'poster.png').resolve()),content_type='image/png'))
                        page.add_init_script('window.Telegram={WebApp:{initData:'+repr(signed_data(user))+',ready(){},expand(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};')
                        page.goto(f'http://127.0.0.1:{port}');page.set_default_timeout(10000)
                        page.locator('#query').fill('Spider-Man');page.locator('#search-submit').click();page.locator('#select').click()
                        expect(page.locator('#radarr-change-profile')).to_be_enabled();expect(page.locator('#radarr-profile')).to_have_text('HD')
                        expect(page.locator('#acquisition-add')).to_have_count(0)
                        page.locator('#radarr-change-profile').click()
                        expect(page.locator('#radarr-profile-choices button')).to_have_count(4)
                        expect(page.locator('#radarr-profile-title')).to_be_focused();expect(page.locator('#radarr-profile-title')).to_have_css('outline-style','none')
                        expect(page.locator('#radarr-profile-choices button[data-profile="2"]')).to_have_attribute('aria-pressed','true')
                        expect(page.locator('#radarr-profile-save')).to_be_disabled()
                        page.locator('#radarr-profile-choices button[data-profile="3"]').click();expect(page.locator('#radarr-profile-save')).to_be_enabled()
                        buttons=[node.bounding_box() for node in page.locator('#radarr-profile-choices button').all()]
                        assert all(buttons[n]['y']+buttons[n]['height']<=buttons[n+1]['y'] for n in range(3))
                        sheet=page.locator('#radarr-profile-dialog').bounding_box();assert sheet['x']>=0 and sheet['x']+sheet['width']<=width
                        page.screenshot(path=str(args.output/f'profile-editor-{width}.png'))
                        page.locator('#radarr-profile-cancel').click();assert native.calls==[] and cat.calls==[]
                        # A single explicit save, followed by verified readback and detail refresh.
                        before=deepcopy(native.value)
                        page.locator('#radarr-change-profile').click();page.locator('#radarr-profile-choices button[data-profile="3"]').click()
                        page.locator('#radarr-profile-save').click();expect(page.locator('#radarr-profile-dialog')).not_to_be_visible()
                        expect(page.locator('#radarr-profile-result')).to_have_text('Profile changed to 4K.',timeout=20000)
                        expect(page.locator('#radarr-profile')).to_have_text('4K')
                        expected=deepcopy(before);expected['qualityProfileId']=3;assert native.value==expected
                        assert native.calls==[('/api/v3/movie/editor',{'movieIds':[10],'qualityProfileId':3})]
                        # Stale native monitoring prevents an unreviewed overwrite.
                        page.locator('#radarr-change-profile').click();page.locator('#radarr-profile-choices button[data-profile="4"]').click()
                        native.value['monitored']=True;page.locator('#radarr-profile-save').click()
                        expect(page.locator('#radarr-profile-save')).to_have_text('Check saved change');assert len(native.calls)==1
                        page.locator('#radarr-profile-cancel').click();native.value['monitored']=False
                        # Lost response/no immediate effect stays recoverable across reopening.
                        page.locator('#radarr-change-profile').click();page.locator('#radarr-profile-choices button[data-profile="4"]').click()
                        native.lost=True;native.effect=False;page.locator('#radarr-profile-save').click()
                        expect(page.locator('#radarr-profile-dialog')).not_to_be_visible()
                        expect(page.locator('#radarr-profile-result')).to_contain_text('saved',timeout=10000)
                        page.locator('#refresh').click();expect(page.locator('#radarr-change-profile')).to_be_enabled()
                        page.locator('#radarr-change-profile').click();expect(page.locator('#radarr-profile-notice')).to_contain_text('already pending')
                        expect(page.locator('#radarr-profile-save')).to_be_disabled();assert len(native.calls)==2
                        native.value['qualityProfileId']=4;native.lost=False;native.effect=True
                        storage=Storage(database)
                        with storage.connection:storage.connection.execute("UPDATE coordination_operation SET retry_at=0,lease_until=NULL WHERE kind='movie_profile' AND status='uncertain'")
                        profile=service(storage,native,cat);identifier=profile.pending('558')[0]['id']
                        assert profile.run(identifier)['status']=='completed';storage.close()
                        expect(page.locator('#radarr-profile-dialog')).not_to_be_visible(timeout=15000)
                        expect(page.locator('#radarr-profile')).to_have_text('Remux',timeout=15000)
                        assert len(native.calls)==2 and native.value['monitored'] is False
                        fixture.ryot.item=None;page.locator('#refresh').click();expect(page.locator('#radarr-change-profile')).to_be_disabled()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');assert not errors,errors
                        context.close();print('Profile editor passed:',width,height,'cancel/save/stale/reopen/recovery and standard-user authorization',flush=True)
                finally:browser.close()
        finally:server.should_exit=True;thread.join(timeout=10);fixture.client.close()


if __name__=='__main__':main()
