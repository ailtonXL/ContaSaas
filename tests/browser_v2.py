"""Teste ponta a ponta da versão 2. Usa banco e servidor descartáveis; não envia mensagens."""
import os
import shutil
import tempfile
import threading
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from playwright.sync_api import sync_playwright

PASSWORD='senha-admin-descartavel'
TEMP_PASSWORD='senha-cliente-temporaria'
CLIENT_PASSWORD='senha-cliente-individual'

with tempfile.TemporaryDirectory() as temporary:
    app.DATA=Path(temporary)
    app.init()
    app.setup(PASSWORD)
    app.seed()
    server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,executable_path=shutil.which('chromium'),args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1440,'height':1000})
            page.set_default_timeout(10000)
            errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            url=f'http://127.0.0.1:{server.server_port}'
            def login(username,password):
                page.goto(url)
                page.get_by_label('Login',exact=True).fill(username)
                page.get_by_label('Senha',exact=True).fill(password)
                page.get_by_role('button',name='Entrar no ContaSaas').click()
            def nav(key):
                page.locator(f'nav [data-page="{key}"]').click()
            def save():
                page.get_by_role('button',name='Salvar',exact=True).click()
                page.locator('dialog').wait_for(state='hidden')
            login('admin',PASSWORD)
            page.get_by_role('heading',name='Tudo sob controle.').wait_for()
            nav('users')
            page.get_by_role('button',name='+ Novo acesso').click()
            page.get_by_label('Nome da pessoa').fill('Cliente Aurora')
            page.get_by_label('Login individual').fill('cliente.aurora')
            page.get_by_label('Perfil',exact=True).select_option('client')
            page.get_by_label('Cliente',exact=True).select_option(label='Aurora Design')
            page.get_by_label('Senha temporária',exact=True).fill(TEMP_PASSWORD)
            save()
            page.get_by_role('cell',name='cliente.aurora',exact=True).wait_for()
            nav('templates')
            page.get_by_role('button',name='+ Nova recorrência').click()
            page.get_by_label('Nome da obrigação').fill('Entrega mensal navegador')
            page.get_by_label('Regime tributário',exact=True).select_option('Simples Nacional')
            page.get_by_label('Dia de vencimento proposto').fill('20')
            page.get_by_label('Responsável',exact=True).fill('Equipe')
            save()
            page.get_by_text('Entrega mensal navegador',exact=True).wait_for()
            nav('obligations')
            page.get_by_role('button',name='Revisar prazo',exact=True).first.click()
            page.get_by_label('Vencimento confirmado').fill('2026-11-23')
            save()
            nav('settings')
            page.get_by_label('Avisar com quantos dias de antecedência?').fill('5')
            page.get_by_label('Repetir a cada quantos dias?').fill('3')
            with page.expect_response(lambda r:r.url.endswith('/api/state')) as updated:
                page.get_by_role('button',name='Salvar automação').click()
            assert updated.value.json()['settings']['days_before']==5
            page.get_by_text('ainda não executado',exact=False).wait_for()
            nav('clients')
            page.get_by_role('button',name='Configurar comunicação').first.click()
            page.get_by_label('WhatsApp com código do país e DDD').fill('5511999999999')
            page.get_by_label('O cliente autorizou os lembretes por WhatsApp').check()
            save()
            nav('reconciliation')
            page.get_by_role('button',name='+ Nova regra').click()
            page.get_by_label('Cliente',exact=True).select_option(label='Aurora Design')
            page.get_by_label('Texto contido no extrato').fill('Licença')
            page.get_by_label('Texto contido na contabilidade').fill('Assinatura')
            save()
            page.get_by_role('button',name='↑ Importar CSV').click()
            page.get_by_label('Cliente',exact=True).select_option(label='Aurora Design')
            page.get_by_label('Formato',exact=True).select_option('ofx')
            ofx='<OFX><BANKID>001<ACCTID>123<STMTTRN><DTPOSTED>20261110<TRNAMT>45.00<FITID>BROWSER-OFX<MEMO>Recebimento OFX navegador</STMTTRN></OFX>'
            page.get_by_label('Arquivo CSV ou OFX').set_input_files({'name':'teste.ofx','mimeType':'application/x-ofx','buffer':ofx.encode()})
            page.get_by_role('button',name='Importar arquivo',exact=True).click()
            page.locator('dialog').wait_for(state='hidden')
            page.locator('.match-row').filter(has_text='Recebimento OFX navegador').wait_for()
            nav('audit')
            page.get_by_role('cell',name='users',exact=True).wait_for()
            Path('/tmp/contasaas-v2-artifacts').mkdir(exist_ok=True)
            page.screenshot(path='/tmp/contasaas-v2-artifacts/auditoria.png',full_page=True)
            page.get_by_role('button',name='Sair do escritório').click()
            login('cliente.aurora',TEMP_PASSWORD)
            page.get_by_role('heading',name='Defina sua nova senha.').wait_for()
            page.get_by_label('Senha atual',exact=True).fill(TEMP_PASSWORD)
            page.get_by_label('Nova senha',exact=True).fill(CLIENT_PASSWORD)
            page.get_by_label('Confirme a nova senha').fill(CLIENT_PASSWORD)
            page.get_by_role('button',name='Atualizar minha senha').click()
            page.get_by_role('heading',name='Olá, Cliente Aurora.').wait_for()
            assert page.locator('nav [data-page="users"]').is_hidden()
            assert page.locator('nav [data-page="clients"]').is_hidden()
            assert page.get_by_text('Café do Centro',exact=True).count()==0
            row=page.get_by_role('row').filter(has_text='Extrato bancário do mês')
            with page.expect_file_chooser() as fc:row.get_by_role('button',name='Anexar',exact=True).click()
            fc.value.set_files({'name':'primeiro.pdf','mimeType':'application/pdf','buffer':b'original'})
            page.get_by_role('link',name='Baixar primeiro.pdf').wait_for()
            row=page.get_by_role('row').filter(has_text='Extrato bancário do mês')
            with page.expect_file_chooser() as fc:row.get_by_role('button',name='Substituir',exact=True).click()
            fc.value.set_files({'name':'segundo.pdf','mimeType':'application/pdf','buffer':b'novo'})
            page.get_by_role('link',name='Baixar segundo.pdf').wait_for()
            page.get_by_role('row').filter(has_text='Extrato bancário do mês').get_by_role('button',name='Versões').click()
            page.get_by_text('primeiro.pdf',exact=True).wait_for()
            assert page.get_by_role('button',name='Restaurar',exact=True).count()==0
            page.get_by_role('button',name='Fechar',exact=True).click()
            page.locator('#toast').evaluate('(element)=>element.hidden=true')
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path='/tmp/contasaas-v2-artifacts/portal-mobile.png',full_page=True)
            page.set_viewport_size({'width':1440,'height':1000})
            page.screenshot(path='/tmp/contasaas-v2-artifacts/portal.png',full_page=True)
            page.get_by_role('button',name='Sair do escritório').click()
            login('admin',PASSWORD)
            page.get_by_role('heading',name='Tudo sob controle.').wait_for()
            nav('documents')
            page.get_by_role('row').filter(has_text='Extrato bancário do mês').get_by_role('button',name='Versões').click()
            page.locator('.version-row').filter(has_text='primeiro.pdf').get_by_role('button',name='Restaurar',exact=True).click()
            page.locator('dialog').wait_for(state='hidden')
            page.get_by_role('link',name='Baixar primeiro.pdf').wait_for()
            assert not errors,errors
            browser.close()
            print('OK: acessos, permissões do portal, troca obrigatória de senha, recorrências/revisão, automações, regras, OFX, versões/restauração, auditoria e celular.')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
