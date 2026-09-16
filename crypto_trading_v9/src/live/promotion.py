"""
واجهة الترقية — يربط live_config بـ monitoring.gates الموجودة.
لا منطق بوابة جديد هنا — فقط قراءة نتيجة الموجود وتمريرها لـ Live.
"""
from typing import Optional
from ..monitoring.gates import evaluate, GateCriteria, PAPER_ONLY


def paper_gate_status(db, criteria: Optional[GateCriteria] = None,
                      tests_passed: Optional[bool] = None) -> bool:
    return evaluate(db, 'paper', criteria, tests_passed, 'paper').passed


def testnet_gate_status(db, criteria: Optional[GateCriteria] = None,
                        tests_passed: Optional[bool] = None) -> bool:
    return evaluate(db, 'testnet', criteria, tests_passed, 'testnet').passed
