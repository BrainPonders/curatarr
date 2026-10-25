"""Browser checks for the minimal maintenance signal and preserved hidden diagnostics."""
import argparse
from dataclasses import replace
import json
import socket
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright, expect
from curatarr.domain import DurableState as State
from curatarr.web.app import create_app
from test_system_maintenance import SystemMaintenanceTests
from test_miniapp import MiniAppTests,signed_data


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='chromium')
        try:
            for width,height in [(390,844),(320,640),(844,390),(1280,900)]:
                fx=SystemMaintenanceTests();fx.setUp();shell=MiniAppTests();shell.setUp()
                service=replace(shell.service,ryot_adapter=fx.ryot,radarr_adapter=fx.radarr)
                app=create_app(shell.config,database=fx.path,service=service,playback_adapter=fx.playback)
                listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
                server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
                thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
                context=browser.new_context(viewport={'width':width,'height':height},color_scheme='dark' if width in (390,844) else 'light')
                context.route('https://telegram.org/js/*',lambda r:r.fulfill(body=''))
                context.add_init_script('window.Telegram={WebApp:{initData:'+repr(signed_data())+',onEvent(){},ready(){},expand(){},BackButton:{show(){},hide(){},onClick(){}}}};')
                def check():
                    row=fx.storage.connection.execute("SELECT id,cursor_json FROM job_run WHERE job_name='system_maintenance' ORDER BY id DESC LIMIT 1").fetchone()
                    if row:
                        payload=json.loads(row['cursor_json']);payload['started_epoch']=time.time()-31
                        with fx.storage.connection:fx.storage.connection.execute('UPDATE job_run SET cursor_json=? WHERE id=?',(json.dumps(payload),row['id']))
                    page.locator('#system-health-check').click()
                    expect(page.locator('#system-health-check')).to_be_enabled(timeout=15000)
                try:
                    deadline=time.monotonic()+10
                    while not server.started:
                        if time.monotonic()>deadline:raise RuntimeError('Server did not start')
                        time.sleep(.02)
                    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{port}');page.locator('#maintenance-toggle').click()
                    expect(page.locator('#system-health')).to_be_visible()
                    expect(page.locator('#maintenance-category')).to_be_hidden()
                    expect(page.locator('#maintenance-movie')).to_be_hidden()
                    check();expect(page.locator('#system-health-value')).to_have_text('No')
                    expect(page.locator('#sync-count')).to_have_text('✓')
                    page.screenshot(path=str(args.output/f'healthy-{width}.png'),full_page=True)
                    fx.playback.error=True;check();expect(page.locator('#system-health-value')).to_have_text('Could not check')
                    expect(page.locator('#sync-count')).to_have_text('?')
                    page.screenshot(path=str(args.output/f'unavailable-{width}.png'),full_page=True)
                    fx.playback.error=False
                    fx.ryot.items[0]=replace(fx.ryot.items[0],durable_states=frozenset({State.LIBRARY,State.RADARR}))
                    check();expect(page.locator('#system-health-value')).to_have_text('Yes')
                    expect(page.locator('#sync-count')).to_have_text('!')
                    expect(page.locator('#maintenance-category')).to_be_hidden()
                    expect(page.get_by_role('button',name='Correct',exact=True)).not_to_be_visible()
                    assert fx.storage.reconciliation_report()['findings'][0]['kind']=='previously_acquired_missing'
                    page.screenshot(path=str(args.output/f'needed-{width}.png'),full_page=True)
                    expect(page.locator('#sync-viewing-open')).to_be_hidden()
                    expect(page.locator('#sync-viewing-panel')).to_be_hidden()
                    page.locator('#settings-branch').click();page.locator('#sync-branch').click()
                    expect(page.locator('#system-health-value')).to_have_text('Yes')
                    assert not errors,errors
                    print(f'PASS {width}x{height}: minimal Yes/No/unknown check, hidden categories retained, technical repairs hidden, branch return',flush=True)
                finally:
                    context.close();server.should_exit=True;thread.join(timeout=5);fx.doCleanups()
        finally:browser.close()

if __name__=='__main__':main()
