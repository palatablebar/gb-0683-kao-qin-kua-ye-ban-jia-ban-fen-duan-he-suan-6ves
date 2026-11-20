import importlib,os
from flask import Flask,jsonify,request,redirect,Response
from app.config import MODULES
from app.errors import DomainError
from app.workflow import run,load
from app import store
from app.web import payload
from app.modules.m7_archive.export_out import batch_bundle, bundle
from app.payroll.batch import replay

def create_app():
    app=Flask(__name__);app.config['MAX_CONTENT_LENGTH']=2_000_000
    for entry in MODULES:app.register_blueprint(importlib.import_module('app.modules.'+entry[0]+'.handler').bp)
    @app.errorhandler(DomainError)
    def domain_error(error):return jsonify(code=error.code,message=error.message),error.status
    @app.errorhandler(404)
    def missing(error):return jsonify(code='NOT_FOUND',message='入口不存在'),404
    @app.get('/')
    def index():return redirect('/'+MODULES[0][0])
    @app.get('/healthz')
    def health():return jsonify(status='ok')
    @app.post('/api/runs')
    def submit():
        content,params=payload();state=run(content,params);return jsonify(store.save(state['run_id'],state))
    @app.get('/api/runs')
    def list_runs():return jsonify(store.history())
    @app.get('/api/runs/<run_id>')
    def get_run(run_id):return jsonify(store.get(run_id))
    @app.post('/api/runs/<run_id>/review')
    def review(run_id):
        data=request.get_json(silent=True) or {}
        if not isinstance(data.get('version'),int):raise DomainError('VERSION','请提交复核版本')
        return jsonify(store.review(run_id,data['version'],data.get('decision')))
    @app.get('/api/archive/<run_id>/export.csv')
    def download(run_id):
        state=store.get(run_id)
        if not state.get('reconcile',{}).get('balanced'):raise DomainError('UNBALANCED','对账未平')
        return Response(bundle(state),mimetype='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename=evidence.csv'})
    @app.get('/api/batches')
    def batch_list():
        return jsonify(store.batches(request.args.get('period')))
    @app.get('/api/batches/<key>')
    def batch_detail(key):
        item=store.batch(key);return jsonify(batch=item,events=store.events(item['key']))
    @app.get('/api/batches/<key>/replay')
    def batch_replay(key):
        return jsonify(replay(store.batch(key)['body']))
    @app.get('/api/batches/<key>/export.csv')
    def batch_export(key):
        item=store.batch(key)
        return Response(batch_bundle(item),mimetype='text/csv; charset=utf-8',
            headers={'Content-Disposition':'attachment; filename='+item['batch_id']+'.csv'})
    @app.post('/api/batches/<key>/seal')
    def batch_seal(key):
        return jsonify(store.seal(key))
    @app.get('/api/periods/<period>/totals')
    def period_totals(period):
        return jsonify(store.period_totals(period))
    with app.app_context():
        if not store.history():
            state=run(load('dirty.csv'),{});store.save(state['run_id'],state)
    return app


app=create_app()
if __name__=='__main__':app.run(host='0.0.0.0',port=8000)
