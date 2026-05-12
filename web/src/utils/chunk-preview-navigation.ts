/** 检索结果点击后：在非结构化 HTML 内按正文片段滚动到视口 */

/** 在指定滚动容器内滚动，使 target 出现在视口上方（不依赖浏览器默认 scrollIntoView，适配 Modal 嵌套） */
export function scrollElementIntoScrollParent(
  scrollParent: HTMLElement | null,
  target: HTMLElement | null,
  behavior: ScrollBehavior = 'smooth'
) {
  if (!scrollParent || !target) return;
  const parentRect = scrollParent.getBoundingClientRect();
  const targetRect = target.getBoundingClientRect();
  const nextTop =
    scrollParent.scrollTop + (targetRect.top - parentRect.top) - 24;
  scrollParent.scrollTo({ top: Math.max(0, nextTop), behavior });
}

export function normalizeWs(s: string): string {
  return s.replace(/\s+/g, ' ').trim();
}

export function chunkDomId(chunkId: string | undefined | null): string {
  const raw = (chunkId || 'unknown').replace(/[^a-zA-Z0-9_-]/g, '_');
  return /^[0-9]/.test(raw) ? `c_${raw}` : raw;
}

export function scrollContainerToChildCenter(
  scrollRoot: HTMLElement,
  target: HTMLElement,
  behavior: ScrollBehavior = 'smooth'
) {
  const rootRect = scrollRoot.getBoundingClientRect();
  const tRect = target.getBoundingClientRect();
  const delta = tRect.top - rootRect.top + scrollRoot.scrollTop - scrollRoot.clientHeight * 0.2;
  scrollRoot.scrollTo({ top: Math.max(0, delta), behavior });
}

/** 在 root 内查找文本片段并滚动 scrollRoot；可选为匹配区间加闪高 mark（surroundContents 可能因跨节点失败） */
export function scrollToSnippetInElement(
  root: HTMLElement,
  scrollRoot: HTMLElement,
  snippet: string,
  options?: { flashClass?: string }
) {
  const s = snippet.trim().slice(0, 120);
  if (!s || s.length < 2) return false;
  const needle = s.slice(0, Math.min(48, s.length));
  const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let n: Node | null;
  while ((n = tw.nextNode())) {
    const t = n.textContent || '';
    const i = t.indexOf(needle);
    if (i < 0) continue;
    
    const currentNode = n;
    if (options?.flashClass && currentNode.parentElement?.classList.contains(options.flashClass)) {
      const parent = currentNode.parentElement;
      scrollContainerToChildCenter(scrollRoot, parent);
      parent.classList.add('chunk-highlight-pulse');
      window.setTimeout(() => parent.classList.remove('chunk-highlight-pulse'), 2000);
      return true;
    }
    
    const range = document.createRange();
    const end = Math.min(i + Math.min(s.length, 240), t.length);
    range.setStart(currentNode, i);
    range.setEnd(currentNode, end);
    try {
      if (options?.flashClass) {
        const mark = document.createElement('mark');
        mark.className = `${options.flashClass} chunk-highlight-pulse`;
        mark.id = 'chunk-snippet-flash';
        range.surroundContents(mark);
        scrollContainerToChildCenter(scrollRoot, mark);
        window.setTimeout(() => {
          mark.classList.remove('chunk-highlight-pulse');
        }, 2000);
        return true;
      }
    } catch {
      /* surroundContents 失败则只滚动 */
    }
    const br = range.getBoundingClientRect();
    const sr = scrollRoot.getBoundingClientRect();
    const top = scrollRoot.scrollTop + br.top - sr.top - scrollRoot.clientHeight * 0.2;
    scrollRoot.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
    return true;
  }
  return false;
}

export function scrollByRatio(scrollRoot: HTMLElement, ratio: number) {
  const r = Math.min(1, Math.max(0, ratio));
  scrollRoot.scrollTo({ top: r * scrollRoot.scrollHeight, behavior: 'smooth' });
}
