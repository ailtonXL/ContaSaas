import base64
import http.cookiejar
import json
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
import app

class ApplicationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_data = app.DATA
        app.DATA = Path(self.tmp.name)
        app.init()
        app.setup('uma-senha-de-teste-segura')
        self.server = app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = ''

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        app.DATA = self.previous_data
        self.tmp.cleanup()

    def request(self,path,data=None,csrf=True,anonymous=False):
        headers = {'Content-Type':'application/json'}
        if csrf:
            headers['X-CSRF-Token'] = self.csrf
        req = urllib.request.Request(self.url+'/api/'+path,data=json.dumps(data).encode() if data is not None else None,headers=headers)
        opener = urllib.request.build_opener() if anonymous else self.client
        try:
            response = opener.open(req)
        except urllib.error.HTTPError as response:
            return response.code,json.loads(response.read())
        raw = response.read()
        return response.status,json.loads(raw) if 'application/json' in response.headers.get('Content-Type','') else raw

    def login(self):
        self.assertEqual(self.request('login',{'password':'uma-senha-de-teste-segura'})[0],200)
        code,state = self.request('state')
        self.assertEqual(code,200)
        self.csrf = state['csrf']

    def client_record(self):
        self.login()
        self.assertEqual(self.request('clients',{'name':'Cliente A','email':'a@example.com','regime':'MEI'})[0],200)
        return self.request('state')[1]['clients'][0]['id']

    def test_auth_csrf_and_logout(self):
        self.assertEqual(self.request('state')[0],401)
        self.assertEqual(self.request('login',{'password':'errada'})[0],401)
        self.login()
        self.assertEqual(self.request('clients',{'name':'X'},csrf=False)[0],403)
        self.assertEqual(self.request('logout',{})[0],200)
        self.assertEqual(self.request('state')[0],401)

    def test_login_throttle(self):
        for _ in range(10):
            self.assertEqual(self.request('login',{'password':'errada'})[0],401)
        self.assertEqual(self.request('login',{'password':'errada'})[0],429)

    def test_documents_upload_download_and_status(self):
        c = self.client_record()
        self.assertEqual(self.request('documents',{'client_id':c,'title':'Extrato','due':str(date.today())})[0],200)
        doc = self.request('state')[1]['documents'][0]
        self.assertEqual(self.request('upload',{'id':doc['id'],'filename':'extrato.pdf','content':base64.b64encode(b'%PDF-test').decode()})[0],200)
        self.assertEqual(self.request(f"download/{doc['id']}",anonymous=True)[0],401)
        self.assertEqual(self.request(f"download/{doc['id']}")[1],b'%PDF-test')
        self.assertEqual(self.request('status',{'table':'documents','id':doc['id'],'status':'Concluído'})[0],200)
        saved=self.request('state')[1]['documents'][0]
        self.assertEqual(saved['status'],'Concluído')
        self.assertNotIn('storage',saved)

    def test_import_atomic_duplicates_and_match(self):
        c = self.client_record()
        csv='data;descricao;valor\n2026-11-10;Serviço;1500,00\n2026-11-10;Serviço;1500,00\n'
        payload={'client_id':c,'kind':'bank','csv':csv}
        self.assertEqual(self.request('import',payload)[0],200)
        self.assertEqual(self.request('import',payload)[0],200)
        self.assertEqual(len(self.request('state')[1]['entries']),2)
        invalid='data;descricao;valor\n2026-11-10;Outra;10,00\ninvalida;Falha;15,00'
        self.assertEqual(self.request('import',{**payload,'csv':invalid})[0],400)
        self.assertEqual(len(self.request('state')[1]['entries']),2)
        self.request('import',{**payload,'kind':'book'})
        entries=self.request('state')[1]['entries']
        banks=[e for e in entries if e['kind']=='bank']
        books=[e for e in entries if e['kind']=='book']
        match={'bank_id':banks[0]['id'],'book_id':books[0]['id']}
        self.assertEqual(self.request('match',match)[0],200)
        self.assertEqual(self.request('match',match)[0],400)
        mid=self.request('state')[1]['matches'][0]['id']
        self.assertEqual(self.request('unmatch',{'id':mid})[0],200)
        self.assertEqual(self.request('state')[1]['matches'],[])

    def test_cross_client_match_rejected(self):
        c=self.client_record()
        self.request('clients',{'name':'Cliente B','email':'b@example.com','regime':'MEI'})
        csv='data;descricao;valor\n2026-11-10;Serviço;12,50'
        for cid,kind in [(c,'bank'),(c+1,'book')]:
            self.request('import',{'client_id':cid,'kind':kind,'csv':csv})
        entries=self.request('state')[1]['entries']
        self.assertEqual(self.request('match',{'bank_id':entries[0]['id'],'book_id':entries[1]['id']})[0],400)

    def test_obligations_and_reminder_idempotence(self):
        c=self.client_record()
        self.assertEqual(self.request('obligations',{'client_id':c,'title':'DAS','due':str(date.today()),'owner':'Maria'})[0],200)
        self.request('reminders',{})
        self.request('reminders',{})
        reminders=self.request('state')[1]['reminders']
        self.assertEqual(len(reminders),1)
        self.assertEqual(reminders[0]['status'],'Na fila')
        self.assertIn('DAS',reminders[0]['body'])
        with app.db() as con:
            self.assertEqual(con.execute('SELECT owner FROM obligations').fetchone()[0],'Maria')

    def test_reminders_cancel_resolved_and_send_via_tls(self):
        c=self.client_record()
        self.request('obligations',{'client_id':c,'title':'DAS','due':str(date.today()),'owner':'Maria'})
        self.request('reminders',{})
        with app.db() as con:
            con.execute("UPDATE obligations SET status='Concluído'")
            app.queue_reminders(con)
            self.assertEqual(con.execute('SELECT status FROM reminders').fetchone()[0],'Cancelado')
            con.execute("UPDATE obligations SET status='Pendente'")
        smtp=MagicMock()
        with patch.dict('os.environ',{'SMTP_HOST':'smtp.example.com','SMTP_USER':'user','SMTP_PASSWORD':'fake','SMTP_FROM':'office@example.com'}), patch('core.smtplib.SMTP',return_value=smtp):
            app.send_reminders()
            smtp.__enter__.return_value.starttls.assert_called_once()
            smtp.__enter__.return_value.send_message.assert_called_once()
        with app.db() as con:
            self.assertEqual(con.execute('SELECT status FROM reminders').fetchone()[0],'Enviado')

    def test_csv_precision_and_format(self):
        for text in ['data;descricao;valor\n2026-11-10;X;1,001','data;descricao;valor\n2026-11-10;X;NaN','data;descricao;valor','errado;descricao;valor\n2026-11-10;X;2']:
            with self.assertRaises(ValueError):
                app.parse_csv(text)
        self.assertEqual(app.parse_csv('data;descricao;valor\n2026-11-10;Saída;-1.234,56')[0][2],-123456)

if __name__=='__main__':
    unittest.main()
