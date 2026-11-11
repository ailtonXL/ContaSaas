import base64
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
import app
import core
import test_app as test_helpers

PASSWORD='senha-temporaria-segura'
NEW_PASSWORD='nova-senha-individual-segura'

class ImprovementsTest(unittest.TestCase):
    setUp=test_helpers.ApplicationTest.setUp
    tearDown=test_helpers.ApplicationTest.tearDown
    request=test_helpers.ApplicationTest.request
    login=test_helpers.ApplicationTest.login
    client_record=test_helpers.ApplicationTest.client_record

    def login_as(self, username, password=PASSWORD, change=False):
        self.assertEqual(self.request('login',{'username':username,'password':password})[0],200)
        self.csrf=self.request('state')[1]['csrf']
        if change:
            self.assertEqual(self.request('password',{'current_password':password,'password':NEW_PASSWORD})[0],200)

    def access(self,role='client',client_id=None,username='cliente'):
        result=self.request('users',{'name':'Pessoa teste','username':username,'role':role,'password':PASSWORD,'client_id':client_id})
        self.assertEqual(result[0],200,result)
        return result[1]['id']

    def doc(self,client_id,title='Documento teste'):
        self.request('documents',{'client_id':client_id,'title':title,'due':str(date.today())})
        return self.request('state')[1]['documents'][-1]['id']

    def upload(self,doc,text='primeiro',filename='arquivo.pdf'):
        return self.request('upload',{'id':doc,'filename':filename,'content':base64.b64encode(text.encode()).decode()})

    def test_individual_password_gate_and_no_secrets_in_state_or_audit(self):
        c=self.client_record()
        self.access(client_id=c)
        self.login_as('cliente')
        self.assertEqual(self.request('state')[1]['user']['must_change'],1)
        self.assertEqual(self.request('state')[1]['clients'],[])
        self.assertEqual(self.request('password',{'current_password':PASSWORD,'password':PASSWORD})[0],400)
        self.assertEqual(self.upload(1)[0],403)
        self.assertEqual(self.request('password',{'current_password':'incorreta','password':NEW_PASSWORD})[0],400)
        self.assertEqual(self.request('password',{'current_password':PASSWORD,'password':NEW_PASSWORD})[0],200)
        self.assertEqual(self.request('state')[1]['user']['must_change'],0)
        self.assertEqual(self.request('login',{'username':'cliente','password':PASSWORD})[0],401)
        self.login_as('cliente',NEW_PASSWORD)
        self.login()
        content=json.dumps(self.request('state')[1])
        for forbidden in ('password_hash','salt',PASSWORD,NEW_PASSWORD):
            self.assertNotIn(forbidden,content)

    def test_portal_scope_and_forbidden_actions(self):
        c=self.client_record()
        own=self.doc(c)
        self.request('clients',{'name':'Outro cliente','email':'outro@example.com','regime':'MEI'})
        other=self.doc(c+1)
        self.upload(other)
        version=self.request('state')[1]['file_versions'][0]['id']
        self.access(client_id=c)
        self.login_as('cliente',change=True)
        state=self.request('state')[1]
        self.assertEqual([r['id'] for r in state['clients']],[c])
        self.assertEqual([r['id'] for r in state['documents']],[own])
        for key in ('users','entries','matches','reminders','audit','templates','match_rules'):
            self.assertEqual(state[key],[])
        self.assertEqual(self.upload(own)[0],200)
        self.assertEqual(self.upload(other)[0],403)
        self.assertEqual(self.request(f'download/{other}')[0],403)
        self.assertEqual(self.request(f'version/{version}')[0],403)
        for route in ('clients','users','status','restore-version','import','settings','rules','templates','reminders','generate','obligation-review','user-update'):
            self.assertEqual(self.request(route,{'id':own})[0],403,route)
        self.assertEqual(self.request('operations')[0],403)

    def test_staff_permissions_and_admin_disable_revokes_session(self):
        self.client_record()
        uid=self.access(role='staff',username='equipe')
        self.login_as('equipe',change=True)
        staff_cookie=self.client
        staff_csrf=self.csrf
        self.assertEqual(self.request('clients',{'name':'Permitido','email':'sim@example.com','regime':'MEI'})[0],200)
        for route in ('users','user-update','settings'):
            self.assertEqual(self.request(route,{})[0],403)
        self.assertEqual(self.request('state')[1]['users'],[])
        import urllib.request
        import http.cookiejar
        self.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.login()
        self.assertEqual(self.request('user-update',{'id':uid,'active':False})[0],200)
        self.client,self.csrf=staff_cookie,staff_csrf
        self.assertEqual(self.request('state')[0],401)
        self.assertEqual(self.request('login',{'username':'equipe','password':NEW_PASSWORD})[0],401)

    def test_office_isolation_on_reads_and_mutations(self):
        c=self.client_record()
        d=self.doc(c)
        self.upload(d)
        self.request('obligations',{'client_id':c,'title':'DAS','due':str(date.today()),'owner':'Maria'})
        self.request('rules',{'client_id':c,'bank_text':'Banco','book_text':'Livro','tolerance':3})
        self.request('templates',{'title':'Mensal','regime':'MEI','due_day':20,'owner':'Maria','start_month':str(date.today())[:7]})
        self.request('reminders',{})
        first=self.request('state')[1]
        app.setup(PASSWORD,'outroadmin','Outro escritório',2)
        self.login_as('outroadmin')
        state=self.request('state')[1]
        self.assertEqual(state['office']['id'],2)
        for key in core.DOMAIN+('file_versions','templates','match_rules'):
            self.assertEqual(state[key],[],key)
        self.assertTrue(all(u['office_id']==2 for u in state['users']))
        self.assertEqual(self.request(f'download/{d}')[0],403)
        self.assertEqual(self.request(f"version/{first['file_versions'][0]['id']}")[0],403)
        forbidden=[('status',{'table':'documents','id':d,'status':'Concluído'}),('upload',{'id':d}),('documents',{'client_id':c}),('obligations',{'client_id':c}),('import',{'client_id':c}),('rules',{'client_id':c}),('restore-version',{'id':first['file_versions'][0]['id']}),('obligation-review',{'id':first['obligations'][0]['id']}),('rule-toggle',{'id':first['match_rules'][0]['id'],'enabled':0}),('template-toggle',{'id':first['templates'][0]['id'],'enabled':0}),('user-update',{'id':first['users'][0]['id'],'active':False}),('client-settings',{'id':c})]
        for route,payload in forbidden:
            self.assertEqual(self.request(route,payload)[0],403,route)
        self.assertEqual(self.request('generate',{})[1]['message'].split()[0],'0')
        self.assertEqual(self.request('reminders',{})[1]['message'].split()[0],'0')

    def test_file_versions_restore_and_audit_actor(self):
        c=self.client_record()
        d=self.doc(c)
        self.upload(d,'original','original.pdf')
        self.upload(d,'substituto','novo.pdf')
        state=self.request('state')[1]
        self.assertEqual(len(state['file_versions']),2)
        original=state['file_versions'][0]
        self.assertEqual(self.request(f'download/{d}')[1],b'substituto')
        self.assertEqual(self.request('restore-version',{'id':original['id']})[0],200)
        self.assertEqual(self.request(f'download/{d}')[1],b'original')
        event=self.request('state')[1]['audit'][0]
        self.assertEqual(event['action'],'restore-version')
        self.assertEqual(event['actor'],'Administrador')
        self.assertEqual(event['user_id'],state['user']['id'])

    @staticmethod
    def ofx(amount='-150.00',fitid='ABC1',day='20261110120000[-3:BRT]'):
        return f'OFXHEADER:100\n<OFX><BANKID>001<ACCTID>123<ACCTTYPE>CHECKING<BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT<DTPOSTED>{day}<TRNAMT>{amount}<FITID>{fitid}<MEMO>Licença &amp; software</STMTTRN></BANKTRANLIST></OFX>'

    def test_ofx_dedup_conflict_and_atomic_import(self):
        c=self.client_record()
        payload={'client_id':c,'kind':'bank','format':'ofx','content':self.ofx()}
        self.assertEqual(self.request('import',payload)[0],200)
        self.request('import',payload)
        entries=self.request('state')[1]['entries']
        self.assertEqual(len(entries),1)
        self.assertEqual(entries[0]['cents'],-15000)
        self.assertEqual(entries[0]['description'],'Licença & software')
        self.assertEqual(self.request('import',{**payload,'content':self.ofx('-151.00')})[0],400)
        self.assertEqual(self.request('import',{**payload,'kind':'book'})[0],400)
        with self.assertRaises(ValueError):core.parse_ofx(self.ofx('NaN'))
        with self.assertRaises(ValueError):core.parse_ofx(self.ofx()+'<STMTTRN><TRNAMT>5')
        with self.assertRaises(ValueError):core.parse_ofx(self.ofx()+'<ACCTID>OutraConta')
        with self.assertRaises(ValueError):core.parse_ofx('<!DOCTYPE x>'+self.ofx())
        xml='<OFX><BANKID>001</BANKID><ACCTID>123</ACCTID><ACCTTYPE>CHECKING</ACCTTYPE><STMTTRN><DTPOSTED>20261110</DTPOSTED><TRNAMT>12.50</TRNAMT><FITID>XML</FITID><NAME>Teste</NAME></STMTTRN></OFX>'
        self.assertEqual(core.parse_ofx(xml)[0][2],1250)

    def test_rule_tolerance_never_auto_confirms(self):
        c=self.client_record()
        for kind,dt,desc in [('bank','2026-11-10','PIX Assinatura 001'),('book','2026-11-12','Licença mensal')]:
            self.request('import',{'client_id':c,'kind':kind,'csv':f'data;descricao;valor\n{dt};{desc};-150,00'})
        self.assertEqual(self.request('state')[1]['suggestions'],[])
        self.request('rules',{'client_id':c,'bank_text':'assinatura','book_text':'LICENÇA','tolerance':3})
        state=self.request('state')[1]
        self.assertEqual(len(state['suggestions']),1)
        self.assertEqual(state['matches'],[])
        self.request('rule-toggle',{'id':state['match_rules'][0]['id'],'enabled':0})
        self.assertEqual(self.request('state')[1]['suggestions'],[])

    def test_recurring_last_day_idempotence_pause_and_review(self):
        c=self.client_record()
        with patch('core.generate_recurring',return_value=0):
            self.request('templates',{'title':'Mensal','regime':'MEI','due_day':31,'owner':'Ana','start_month':'2027-02'})
        with app.db() as con:
            self.assertEqual(core.generate_recurring(con,1,on=date(2027,2,1)),2)
            self.assertEqual(core.generate_recurring(con,1,on=date(2027,2,1)),0)
            rows=con.execute('SELECT * FROM obligations ORDER BY due').fetchall()
            self.assertEqual([r['due'] for r in rows],['2027-02-28','2027-03-31'])
            self.assertTrue(all(r['deadline_reviewed']==0 for r in rows))
        self.assertEqual(self.request('obligation-review',{'id':rows[0]['id'],'due':'2027-02-26','owner':'Ana'})[0],200)
        self.assertEqual(self.request('state')[1]['obligations'][0]['deadline_reviewed'],1)
        self.request('template-toggle',{'id':rows[0]['template_id'],'enabled':False})
        with app.db() as con:
            self.assertEqual(core.generate_recurring(con,1,on=date(2027,4,1)),0)

    def test_reminder_settings_channels_interval_and_resolved(self):
        c=self.client_record()
        d=self.doc(c)
        self.request('client-settings',{'id':c,'phone':'5511999999999','whatsapp_opt_in':True,'reminders_enabled':True})
        self.request('settings',{'enabled':True,'days_before':3,'interval_days':3,'email_enabled':True,'whatsapp_enabled':True})
        with app.db() as con:
            self.assertEqual(core.queue_reminders(con,automatic=True),1)
            row=con.execute('SELECT * FROM reminders').fetchone()
            self.assertEqual(row['whatsapp_status'],'Na fila')
            con.execute("UPDATE reminders SET status='Enviado',email_status='Enviado',whatsapp_status='Enviado'")
            self.assertEqual(core.queue_reminders(con,on=date.today()+timedelta(days=1)),0)
            self.assertEqual(core.queue_reminders(con,on=date.today()+timedelta(days=3)),1)
            con.execute("UPDATE documents SET status='Concluído'")
            core.queue_reminders(con,on=date.today()+timedelta(days=3))
            self.assertEqual(con.execute('SELECT status FROM reminders ORDER BY day DESC').fetchone()[0],'Cancelado')
        # Sem autorização, o canal WhatsApp não entra na fila.
        self.request('client-settings',{'id':c,'phone':'5511999999999','whatsapp_opt_in':False,'reminders_enabled':True})
        with app.db() as con:
            con.execute('DELETE FROM reminders')
            con.execute("UPDATE documents SET status='Pendente'")
            core.queue_reminders(con)
            self.assertEqual(con.execute('SELECT whatsapp_status FROM reminders').fetchone()[0],'Desativado')
        self.upload(d)
        with app.db() as con:
            core.queue_reminders(con)
            self.assertEqual(con.execute('SELECT status FROM reminders').fetchone()[0],'Cancelado')

    def test_worker_opt_in_and_independent_channel_retry(self):
        c=self.client_record()
        self.doc(c)
        self.request('client-settings',{'id':c,'phone':'5511999999999','whatsapp_opt_in':True,'reminders_enabled':True})
        with patch('core.send_email') as email,patch('core.send_whatsapp') as whatsapp:
            self.assertEqual(core.send_reminders(app.DATA,automatic=True),0)
            email.assert_not_called()
            whatsapp.assert_not_called()
            self.request('settings',{'enabled':True,'days_before':7,'interval_days':1,'email_enabled':True,'whatsapp_enabled':True})
            whatsapp.side_effect=ValueError('simulado')
            self.assertEqual(core.send_reminders(app.DATA,automatic=True),1)
            self.assertEqual(email.call_count,1)
            whatsapp.side_effect=None
            self.assertEqual(core.send_reminders(app.DATA,automatic=True),0)
            with app.db() as con:
                con.execute('UPDATE reminders SET last_attempt=0')
            self.assertEqual(core.send_reminders(app.DATA,automatic=True),1)
            self.assertEqual(email.call_count,1)
            self.assertEqual(whatsapp.call_count,2)
            self.assertEqual(self.request('state')[1]['reminders'][0]['status'],'Enviado')
        with app.db() as con:
            token=core.acquire_lease(con,'reminders')
            self.assertIsNotNone(token)
        self.assertEqual(core.send_reminders(app.DATA),0)

    def test_whatsapp_template_request_is_mocked(self):
        row={'phone':'5511999999999','name':'Empresa','body':'Olá\n\nPendências\n\nDocumento: extrato\n\nAcesse o portal'}
        response=MagicMock()
        response.__enter__.return_value=io.BytesIO(b'{"messages":[{"id":"test"}]}')
        with patch.dict('os.environ',{'WHATSAPP_TOKEN':'token-ficticio','WHATSAPP_PHONE_ID':'123','WHATSAPP_TEMPLATE':'pendencias'}),patch('core.urllib.request.urlopen',return_value=response) as request:
            core.send_whatsapp(row)
            sent=request.call_args.args[0]
            self.assertEqual(sent.full_url,'https://graph.facebook.com/v23.0/123/messages')
            payload=json.loads(sent.data)
            self.assertEqual(payload['type'],'template')
            self.assertEqual(payload['template']['components'][0]['parameters'][0]['text'],'Empresa')

    def test_backup_restore_integrity_versions_and_sessions(self):
        c=self.client_record()
        d=self.doc(c)
        self.upload(d,'original')
        self.upload(d,'novo')
        name=core.create_backup(app.DATA)
        destination=app.DATA/'restaurado'
        core.restore_backup(app.DATA/'backups'/name,destination)
        with core.connect(destination) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM clients').fetchone()[0],1)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],0)
            versions=con.execute('SELECT * FROM file_versions ORDER BY id').fetchall()
            self.assertEqual(len(versions),2)
            self.assertEqual((destination/'uploads'/versions[0]['storage']).read_bytes(),b'original')
            self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        with self.assertRaises(ValueError):
            core.restore_backup(app.DATA/'backups'/name,destination)
        corrupt=app.DATA/'corrupt.zip'
        with zipfile.ZipFile(app.DATA/'backups'/name) as src,zipfile.ZipFile(corrupt,'w') as out:
            for key in src.namelist():
                out.writestr(key,b'corrompido' if key=='contasaas.db' else src.read(key))
        with self.assertRaises(ValueError):
            core.restore_backup(corrupt,app.DATA/'falha')
        self.assertFalse((app.DATA/'falha'/'contasaas.db').exists())

    def test_worker_creates_daily_backup_and_recurring_without_messages(self):
        c=self.client_record()
        self.request('templates',{'title':'Mensal','regime':'MEI','due_day':20,'owner':'Equipe','start_month':str(date.today())[:7]})
        with patch('core.send_email') as email,patch('core.send_whatsapp') as whatsapp:
            core.run_tick(app.DATA)
            core.run_tick(app.DATA)
            email.assert_not_called()
            whatsapp.assert_not_called()
        self.assertEqual(len(list((app.DATA/'backups').glob('*.zip'))),1)
        with app.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM obligations').fetchone()[0],2)
            self.assertIsNotNone(con.execute('SELECT last_success FROM worker_status').fetchone()[0])
            self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_restore_rejects_path_traversal(self):
        archive=app.DATA/'unsafe.zip'
        payload=b'unsafe'
        with zipfile.ZipFile(archive,'w') as out:
            hashes={'contasaas.db':hashlib.sha256(payload).hexdigest(),'../outside':hashlib.sha256(payload).hexdigest()}
            out.writestr('manifest.json',json.dumps({'version':2,'sha256':hashes}))
            out.writestr('contasaas.db',payload)
            out.writestr('../outside',payload)
        with self.assertRaises(ValueError):core.restore_backup(archive,app.DATA/'destination')
        self.assertFalse((app.DATA/'outside').exists())

    def test_migration_preserves_legacy_data_and_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            con=sqlite3.connect(folder/'contasaas.db')
            con.row_factory=sqlite3.Row
            con.executescript(app.SCHEMA)
            con.execute("INSERT INTO clients(name,email) VALUES('Legado','legado@example.com')")
            con.execute("INSERT INTO documents(client_id,title,due) VALUES(1,'Documento legado','2026-11-20')")
            con.commit()
            salt='1'*32
            (folder/'auth.json').write_text(json.dumps({'salt':salt,'hash':core.password_hash(PASSWORD,salt)}))
            core.migrate(con,folder)
            core.migrate(con,folder)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM clients').fetchone()[0],1)
            self.assertEqual(con.execute('SELECT office_id FROM documents').fetchone()[0],1)
            user=con.execute('SELECT * FROM users').fetchone()
            self.assertEqual(user['username'],'admin')
            self.assertEqual(user['password_hash'],core.password_hash(PASSWORD,user['salt']))
            con.close()

if __name__=='__main__':unittest.main()
