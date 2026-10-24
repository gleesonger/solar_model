from __future__ import annotations


# Official Sigen Energy Controller nameplate maximum PV input powers:
# https://www.sigenergy.com/en/support/files/1156
SigenStorModelSpecification = tuple[str, str, float]
SIGENSTOR_MODEL_SPECIFICATIONS: list[SigenStorModelSpecification] = [
    ("SigenStor EC 3.0 SP",    "EC 3.0 SP",    6.0),
    ("SigenStor EC 3.6 SP",    "EC 3.6 SP",    7.36),
    ("SigenStor EC 4.0 SP",    "EC 4.0 SP",    8.0),
    ("SigenStor EC 4.6 SP",    "EC 4.6 SP",    9.2),
    ("SigenStor EC 5.0 SP",    "EC 5.0 SP",   10.0),
    ("SigenStor EC 6.0 SP",    "EC 6.0 SP",   12.0),
    ("SigenStor EC 8.0 SP",    "EC 8.0 SP",   16.0),
    ("SigenStor EC 10.0 SP",   "EC 10.0 SP",  20.0),
    ("SigenStor EC 12.0 SP",   "EC 12.0 SP",  24.0),
    ("SigenStor EC 5.0 TP",    "EC 5.0 TP",    8.0),
    ("SigenStor EC 6.0 TP",    "EC 6.0 TP",    9.6),
    ("SigenStor EC 8.0 TP",    "EC 8.0 TP",   12.8),
    ("SigenStor EC 10.0 TP",   "EC 10.0 TP",  16.0),
    ("SigenStor EC 12.0 TP",   "EC 12.0 TP",  19.2),
    ("SigenStor EC 15.0 TP",   "EC 15.0 TP",  24.0),
    ("SigenStor EC 17.0 TP",   "EC 17.0 TP",  27.2),
    ("SigenStor EC 20.0 TP",   "EC 20.0 TP",  32.0),
    ("SigenStor EC 25.0 TP",   "EC 25.0 TP",  40.0),
    ("SigenStor EC 30.0 TP",   "EC 30.0 TP",  48.0),
    ("SigenStor EC 5.0 TPLV",  "EC 5.0 TPLV",  8.0),
    ("SigenStor EC 6.0 TPLV",  "EC 6.0 TPLV",  9.6),
    ("SigenStor EC 8.0 TPLV",  "EC 8.0 TPLV", 12.8),
    ("SigenStor EC 10.0 TPLV", "EC 10.0 TPLV", 16.0),
    ("SigenStor EC 12.0 TPLV", "EC 12.0 TPLV", 19.2),
]
