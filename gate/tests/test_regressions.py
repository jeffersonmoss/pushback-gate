"""Bounded regressions. Run in a disposable filesystem sandbox; never executes push/send/delete commands."""
import contextlib, io, json, os, pathlib, subprocess, sys, tempfile, time, unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.dont_write_bytecode=True
os.environ["PUSHBACK_GATE_NO_LOCAL"]="1"  # never use the machine's local policy in tests
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import gate
import gatelib as G
import review_worker as W

class Regressions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pushback-regression-')
        self.base=pathlib.Path(self.temp.name);self.root=self.base/'repo';self.root.mkdir()
        (self.base/'TEMP_README.md').write_text('owner: gate regressions\nshared: no\npurpose: synthetic test fixtures\nneeded: until this test ends\ncostly to recreate: no\n')
        self.git('init','-q');self.git('config','core.hooksPath','/dev/null')
        (self.root/'app.py').write_text('original\n');self.git('add','app.py');self.git('-c','user.name=test','-c','user.email=test@local','commit','-qm','initial')
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close);self.addCleanup(self.temp.cleanup)
        for key,value in {'STATE_ROOT':str(self.base/'state'),'SESSIONS_DIR':str(self.base/'state/sessions'),'LOG_PATH':str(self.base/'state/gate.log')}.items():self.stack.enter_context(patch.object(G,key,value))
        self.stack.enter_context(patch.dict(os.environ,{'PUSHBACK_GATE_INTERNAL':'0','PUSHBACK_GATE_DISABLED':'0'}))
        self.stack.enter_context(patch.object(gate,'subprocess',SimpleNamespace(Popen=lambda *a,**k:SimpleNamespace(pid=0),DEVNULL=subprocess.DEVNULL,STDOUT=subprocess.STDOUT)))
        self.p=G.load_policy()
    def git(self,*args):
        return subprocess.check_output(['git',*args],cwd=self.root,text=True).rstrip('\n')
    def seed(self,client='claude',**kw):
        g={'gen':1,'status':'done','decision':'CONSULT','verdict':'STOP','goal':'Faster search','context_status':'MATCH','root':str(self.root),'cwd':str(self.root),'prompt_head':'Add a second database','deadline':time.time()-10,'late':False,'delivered':True,'mode':'fast','restorable':True,'stop_blocks':0}
        g.update(kw);G.SessionState(client,'test').update(lambda s:s.update({'generation':1,'generations':{'1':g}}));return g
    def current(self,client='claude'):return G.SessionState(client,'test').current()
    def hook(self,event,payload,client='claude'):
        data={'session_id':'test','cwd':str(self.root),**payload};out=io.StringIO()
        with patch.object(sys,'argv',['gate.py',client,event]),patch.object(sys,'stdin',io.StringIO(json.dumps(data))),contextlib.redirect_stdout(out):gate.main()
        return json.loads(out.getvalue()) if out.getvalue() else None
    def mutation(self,client='claude',**payload):return self.hook('PreToolUse',{'tool_name':'Edit','tool_input':{'file_path':str(self.root/'app.py')},**payload},client)
    def denied(self,out):return bool(out and out.get('hookSpecificOutput',{}).get('permissionDecision')=='deny')
    def test_timely_stop_survives_deadline_both_clients(self):
        for client in ['claude','codex']:
            self.seed(client);self.assertTrue(self.denied(self.mutation(client)))
    def test_acknowledgements_and_questions_preserve_stop(self):
        for prompt in ['Thanks','got it','understood','What is the objection?']:
            self.seed();self.hook('UserPromptSubmit',{'prompt':prompt});self.assertEqual(G.SessionState('claude','test').read()['generation'],1);self.assertTrue(self.denied(self.mutation()))
    def test_informed_override_and_correction_do_not_reconsult(self):
        for prompt in ["I've read the objection; proceed with Postgres",'Do it anyway','No, use the existing index','yes','Go ahead','Continue','okay']:
            self.seed();self.hook('UserPromptSubmit',{'prompt':prompt});self.assertEqual(self.current()['status'],'skipped');self.assertIsNone(self.mutation())
    def test_ack_preserves_pending_without_new_context(self):
        self.seed(status='pending',deadline=time.time()+100)
        self.assertIsNone(self.hook('UserPromptSubmit',{'prompt':'Thanks'}));self.assertEqual(G.SessionState('claude','test').read()['generation'],1)
    def test_cancel_silent_and_no_worker(self):
        self.seed(status='pending',deadline=time.time()+100)
        self.assertIsNone(self.hook('UserPromptSubmit',{'prompt':'Cancel that change immediately'}));self.assertEqual(self.current()['status'],'skipped')
    def test_no_public_preamble_and_context_not_constraint(self):
        out=self.hook('UserPromptSubmit',{'prompt':'Add a second database'})['hookSpecificOutput']['additionalContext']
        self.assertIn('internally',out);self.assertIn('casual context',out);self.assertNotIn('Before you start, state',out)
    def test_short_polite_and_compound_requests(self):
        for s in ['Install x','Can you add a second database?','Inspect app.py then install a database package','Yes, add a second database']:
            self.assertEqual(G.classify_rules(s,self.p)[0],'CONSULT',s)
    def test_benign_requests_do_not_get_consult(self):
        for s in ['Explain the architecture','What is a database?','Should we add a second database?','Fix the typo in README','Thanks']:
            self.assertEqual(G.classify_rules(s,self.p)[0],'SKIP',s)
    def test_wait_first_not_skipped_and_deadline_reachable(self):
        start=time.time();self.hook('UserPromptSubmit',{'prompt':'Review this first, then migrate the database'})
        g=self.current();self.assertEqual((g['mode'],g['decision']),('wait','CONSULT'));self.assertGreater(g['deadline']-start,239)
        self.seed(status='pending',mode='wait',deadline=start+240,started_ts=start)
        with patch.object(G,'now_ts',return_value=start+181):self.assertTrue(self.denied(self.mutation()))
        with patch.object(G,'now_ts',return_value=start+241):self.assertFalse(self.denied(self.mutation()))
    def test_classification_and_preparation_hold_all_mutations(self):
        for status in ['classifying','preparing']:
            self.seed(status=status,deadline=time.time()+100)
            for tool,inp in [('Edit',{'file_path':str(self.root/'app.py')}),('apply_patch',{'patch':'synthetic'}),('Bash',{'command':'python3 change.py'})]:
                self.assertTrue(self.denied(self.hook('PreToolUse',{'tool_name':tool,'tool_input':inp})))
            self.assertIsNone(self.hook('PreToolUse',{'tool_name':'Read','tool_input':{}}))
    def test_failure_visible_once_without_permission_request(self):
        self.seed(status='failed',error='synthetic failure');out=self.mutation();self.assertIn('review FAILED',out['hookSpecificOutput']['additionalContext']);self.assertFalse(self.denied(out));self.assertIsNone(self.mutation())
    def test_wrong_or_missing_goal_is_advisory(self):
        for change in [{'goal':''},{'context_status':'WRONG-REPOSITORY'},{'context_status':'UNCERTAIN'}]:
            self.seed(delivered=False,**change);self.assertFalse(self.denied(self.mutation()))
    def test_changed_cwd_does_not_bypass_reviewed_target(self):
        self.seed();self.assertTrue(self.denied(self.mutation(cwd=str(self.base))))
        out=self.hook('PreToolUse',{'cwd':str(self.base),'tool_name':'Bash','tool_input':{'command':'git -C '+str(self.root)+' push'}})
        self.assertTrue(self.denied(out))
    def test_explicit_other_target_and_read_do_not_get_unrelated_veto(self):
        self.seed();out=self.mutation(tool_input={'file_path':str(self.base/'other.py')});self.assertFalse(self.denied(out));self.assertIn('different repository',out['hookSpecificOutput']['additionalContext'])
        self.seed();self.assertIsNone(self.hook('PreToolUse',{'tool_name':'Read','tool_input':{'file_path':str(self.base/'other.py')}}))
    def test_undelivered_stop_acknowledgement_does_not_override(self):
        for prompt in ['yes','Continue','Thanks']:
            self.seed(delivered=False);self.hook('UserPromptSubmit',{'prompt':prompt});self.assertTrue(self.denied(self.mutation()))
    def test_routine_mentions_of_architecture_do_not_consult(self):
        for prompt in ['Fix the typo in the migration docstring','Run the migration tests','Update the README architecture section','Review the migration plan','Commit the schema change']:
            self.assertEqual(G.classify_rules(prompt,self.p)[0],'SKIP',prompt)
        for prompt in ['Run tests then install redis','Review the migration plan and migrate the database']:
            self.assertEqual(G.classify_rules(prompt,self.p)[0],'CONSULT',prompt)
    def test_decorated_context_tokens_are_normalized(self):
        for value,want in [('Match','MATCH'),('MATCH.','MATCH'),('MATCH (verified files)','MATCH'),('Wrong-Repository.','WRONG-REPOSITORY')]:
            self.assertEqual(G.parse_verdict('VERDICT: STOP\nGOAL: x\nCONTEXT: '+value)['context'],want)
    def test_small_shape_departure_has_no_stop_ritual(self):
        a,_,_=G.checkpoint(str(self.root),'before',self.p);self.seed(verdict='GO',checkpoint=a,shape='files: app.py\nexpected size: small')
        (self.root/'test_extra.py').write_text('assert True\n');self.assertIsNone(self.hook('Stop',{}));self.assertTrue(self.current()['diff_checked'])
    def test_late_stop_remains_advisory(self):
        self.seed(late=True,delivered=False);self.assertFalse(self.denied(self.mutation()))
    def test_go_no_stop_ceremony(self):
        self.seed(verdict='GO',delivered=False);self.assertIsNone(self.hook('Stop',{}));self.assertTrue(self.current()['delivered'])
    def test_git_global_options_and_shell_wrappers(self):
        for cmd in ['git -C /tmp push','git -c x=y push','git --git-dir=/tmp/x push','git --work-tree /tmp push','git --no-pager push','bash -c "git push"','sh -lc "git -C /tmp push"','git branch -D branch','git reset --hard']:
            self.assertEqual(G.bash_kind(cmd,self.p),'irreversible',cmd)
        for cmd in ['git -C /tmp status','git --no-pager log','git -c color.ui=false diff']:
            self.assertEqual(G.bash_kind(cmd,self.p),'read',cmd)
        self.assertNotEqual(G.bash_kind('git unknown-alias',self.p),'read')
    def test_checkpoint_refs_do_not_collide(self):
        a,ra,_=G.checkpoint(str(self.root),'consult-1',self.p);(self.root/'app.py').write_text('beta\n');b,rb,_=G.checkpoint(str(self.root),'consult-1',self.p)
        self.assertNotEqual(ra,rb);self.assertEqual(self.git('rev-parse',ra),a);self.assertEqual(self.git('rev-parse',rb),b)
    def test_restore_retains_unique_unstaged_data_and_index(self):
        a,_,_=G.checkpoint(str(self.root),'before',self.p)
        (self.root/'app.py').write_text('staged\n');self.git('add','app.py');(self.root/'app.py').write_text('unique unstaged\n');(self.root/'new file.txt').write_text('new\n')
        index=(self.root/'.git/index').read_bytes();head=self.git('rev-parse','HEAD')
        ok,note=G.restore(str(self.root),a);self.assertTrue(ok,note);self.assertEqual((self.root/'app.py').read_text(),'original\n');self.assertEqual((self.root/'.git/index').read_bytes(),index);self.assertEqual(self.git('rev-parse','HEAD'),head)
        import re
        recovery=re.search(r'recovery snapshot ([0-9a-f]+)',note).group(1)
        self.assertEqual(self.git('show',recovery+':app.py'),'unique unstaged');self.assertEqual(self.git('show',recovery+':new file.txt'),'new')
        ok,note=G.restore(str(self.root),recovery);self.assertTrue(ok,note);self.assertEqual((self.root/'app.py').read_text(),'unique unstaged\n')
    def test_restore_aborts_if_recovery_fails(self):
        a,_,_=G.checkpoint(str(self.root),'before',self.p);(self.root/'app.py').write_text('unique\n')
        with patch.object(G,'_checkpoint_locked',return_value=(None,None,'synthetic disk failure')):ok,note=G.restore(str(self.root),a)
        self.assertFalse(ok);self.assertEqual((self.root/'app.py').read_text(),'unique\n')
    def test_restore_refuses_symlink_target_without_modification(self):
        (self.root/'link').symlink_to('app.py');a,_,_=G.checkpoint(str(self.root),'symlink',self.p);(self.root/'app.py').write_text('new\n')
        ok,note=G.restore(str(self.root),a);self.assertFalse(ok);self.assertIn('symlink',note);self.assertEqual((self.root/'app.py').read_text(),'new\n')
    def test_no_automatic_git_init(self):
        plain=self.base/'plain';plain.mkdir();(plain/'x.py').write_text('x=1')
        a,_,note=G.checkpoint(str(plain),'plain',self.p);self.assertIsNone(a);self.assertFalse((plain/'.git').exists())
    def test_classifier_isolated_and_goal_context_parsed(self):
        with patch.object(W,'run',return_value=(0,'SKIP fast harmless','')) as run:
            W.classify_with_model('Make it friendlier',self.p)
            args=run.call_args.args[0];self.assertIn('--safe-mode',args);self.assertIn('--strict-mcp-config',args)
        p=G.parse_verdict('VERDICT: STOP\nGOAL: x\nCONTEXT: WRONG-REPOSITORY\nNOTES: x');self.assertEqual(p['context'],'WRONG-REPOSITORY')
    def test_existing_untracked_file_not_spurious_departure(self):
        (self.root/'preexisting.py').write_text('keep\n');a,_,_=G.checkpoint(str(self.root),'before',self.p)
        result=G.diff_check(str(self.root),a,'files: app.py\nexpected size: small',self.p)
        self.assertNotIn('preexisting.py',result['unexpected_files']);self.assertEqual(result['lines'],0)

if __name__=='__main__':unittest.main(verbosity=2)
