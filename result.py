# -*- coding: utf-8 -*-
from enum import Enum

class SolveStatus(Enum):
    SUCCESS = "success"
    EXPIRED = "expired"
    FAILED = "failed"
    BLIND = "blind"
