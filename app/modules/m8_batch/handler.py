from flask import Blueprint
from app.web import api, page
bp = Blueprint('m8_batch', __name__)


@bp.get('/m8_batch')
def screen():
    return page(8, 'm8_batch.html')


@bp.route('/api/m8_batch', methods=['GET', 'POST'])
def preview():
    return api(8)
