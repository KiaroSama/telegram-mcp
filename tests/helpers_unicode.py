"""Unicode fixtures shared by the message-view and text-fidelity tests.

One definition each: a test asserting a ZWNJ survives proves nothing if a second
copy of the constant lost it.
"""

PERSIAN = "\u0645\u06cc\u200c\u06a9\u0646\u062f"  # mi-konad, needs its ZWNJ
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # one emoji, held by ZWJ
HOSTILE = "\u202eevil\u200b\u2062\ufff9"  # RLO + hidden padding
# An emoji tag sequence: flag base + "gbsct" + cancel.
FLAG = "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f"
