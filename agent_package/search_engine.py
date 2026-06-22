"""混合搜索引擎 - TF-IDF 语义(70%) + BM25 关键词(30%) 零外部依赖"""
import math
import re
from collections import Counter
from pathlib import Path
from datetime import datetime, timedelta
from typing import List


class HybridSearchEngine:
    """混合搜索：最终得分 = TF-IDF向量分 x 0.7 + BM25关键词分 x 0.3"""

    VECTOR_WEIGHT = 0.7
    KEYWORD_WEIGHT = 0.3

    def __init__(self, workspace_dir: Path):
        self.workspace = Path(workspace_dir)
        self.memory_dir = self.workspace / "memory"
        self._documents: List[dict] = []
        self._index_built = False

    def _tokenize(self, text: str) -> List[str]:
        return re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", text.lower())

    def build_index(self):
        self._documents = []
        memory_path = self.workspace / "MEMORY.md"
        if memory_path.exists():
            content = memory_path.read_text(encoding="utf-8")
            for i, section in enumerate(re.split(r"\n(?=### )", content)):
                if section.strip():
                    self._documents.append({"id": f"mem_{i}", "source": "MEMORY.md", "content": section.strip(), "date": ""})
        for i in range(30):
            date_str = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            log_path = self.memory_dir / f"{date_str}.md"
            if log_path.exists():
                content = log_path.read_text(encoding="utf-8")
                for j, section in enumerate(re.split(r"\n(?=### )", content)):
                    if section.strip():
                        self._documents.append({"id": f"log_{date_str}_{j}", "source": f"memory/{date_str}.md", "content": section.strip(), "date": date_str})
        self._index_built = True

    def _bm25_score(self, query_terms, doc_terms, avg_dl, k1=1.5, b=0.75):
        doc_len = len(doc_terms)
        doc_counter = Counter(doc_terms)
        if doc_len == 0:
            return 0.0
        score = 0.0
        for term in query_terms:
            if term not in doc_counter:
                continue
            tf = doc_counter[term]
            df = sum(1 for d in self._documents if term in d["content"].lower())
            idf = math.log((len(self._documents) - df + 0.5) / (df + 0.5) + 1.0)
            score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * doc_len / avg_dl))
        return score

    def _tfidf_cosine(self, query_terms, doc_terms):
        query_tf = Counter(query_terms)
        doc_tf = Counter(doc_terms)
        all_terms = set(query_tf.keys()) | set(doc_tf.keys())
        N = max(len(self._documents), 1)
        idf = {}
        for term in all_terms:
            df = sum(1 for d in self._documents if term in d["content"].lower())
            idf[term] = math.log((N + 1) / (df + 1)) + 1
        dot = norm_q = norm_d = 0.0
        for term in all_terms:
            q_val = query_tf.get(term, 0) * idf[term]
            d_val = doc_tf.get(term, 0) * idf[term]
            dot += q_val * d_val
            norm_q += q_val ** 2
            norm_d += d_val ** 2
        if norm_q == 0 or norm_d == 0:
            return 0.0
        return dot / (math.sqrt(norm_q) * math.sqrt(norm_d))

    def search(self, query: str, top_k: int = 5) -> List[dict]:
        if not self._index_built:
            self.build_index()
        if not self._documents:
            return []
        query_terms = self._tokenize(query)
        if not query_terms:
            return []
        doc_terms_list = [self._tokenize(d["content"]) for d in self._documents]
        avg_dl = sum(len(dt) for dt in doc_terms_list) / max(len(doc_terms_list), 1)
        scored = []
        for i, doc in enumerate(self._documents):
            vector_score = self._tfidf_cosine(query_terms, doc_terms_list[i])
            keyword_score = self._bm25_score(query_terms, doc_terms_list[i], avg_dl)
            keyword_norm = 1.0 / (1.0 + math.exp(-keyword_score / 2))
            combined = vector_score * self.VECTOR_WEIGHT + keyword_norm * self.KEYWORD_WEIGHT
            if combined > 0.01:
                scored.append({"source": doc["source"], "content": doc["content"][:600], "score": round(combined, 4), "vector_score": round(vector_score, 4), "keyword_score": round(keyword_norm, 4), "date": doc.get("date", "")})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def search_context(self, query: str, top_k: int = 3) -> str:
        """搜索并返回结构化上下文，可直接注入 system prompt"""
        results = self.search(query, top_k)
        if not results:
            return ""
        lines = ["\n## 相关记忆（按相关性排序）"]
        for i, r in enumerate(results, 1):
            date_info = f" | {r['date']}" if r.get("date") else ""
            lines.append(f"{i}. [来源: {r['source']}{date_info}] [相关性: {r['score']:.0%}]\n   {r['content'][:250]}")
        return "\n".join(lines)
