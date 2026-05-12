#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import re

# Fallback implementation when infinity module is not available
class RagTokenizer:
    """Simple fallback tokenizer when infinity is not available"""

    def __init__(self):
        self._tradi2simp = {}
        self._strQ2B = strQ2B

    def tokenize(self, line: str) -> str:
        """Simple tokenization - split by whitespace and punctuation"""
        if not line:
            return ""
        # Simple tokenization: split by whitespace and common punctuation
        tokens = re.findall(r'[\w]+|[^\w\s]', line, re.UNICODE)
        return " ".join(tokens)

    def fine_grained_tokenize(self, tks: str) -> str:
        """Fine-grained tokenization"""
        if not tks:
            return ""
        # For simplicity, just return the input
        return tks

    def tag(self, line: str):
        """POS tagging - not implemented in fallback"""
        return []

    def freq(self, tk: str) -> int:
        """Return frequency of token"""
        return 1


def strQ2B(s: str) -> str:
    """Convert full-width characters to half-width"""
    result = []
    for char in s:
        code = ord(char)
        # Full-width space
        if code == 0x3000:
            result.append(' ')
        # Full-width characters (except space)
        elif 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        else:
            result.append(char)
    return ''.join(result)


def is_chinese(s):
    """Check if string contains Chinese characters"""
    if not s:
        return False
    for ch in s:
        if '\u4e00' <= ch <= '\u9fff':
            return True
    return False


def is_number(s):
    """Check if string is a number"""
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def is_alphabet(s):
    """Check if string contains only alphabetic characters"""
    if not s:
        return False
    return s.isalpha()


def naive_qie(txt):
    """Simple word segmentation"""
    if not txt:
        return []
    # Simple segmentation by whitespace
    return txt.split()


tokenizer = RagTokenizer()
tokenize = tokenizer.tokenize
fine_grained_tokenize = tokenizer.fine_grained_tokenize
tag = tokenizer.tag
freq = tokenizer.freq
tradi2simp = tokenizer._tradi2simp
strQ2B = strQ2B
