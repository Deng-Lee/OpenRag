import { pdfjs } from 'react-pdf';

let initialized = false;

export function initPdfWorker(): void {
  if (initialized) return;
  // 离线环境不能依赖 CDN，worker 必须走本地打包产物
  pdfjs.GlobalWorkerOptions.workerSrc = new URL(
    'pdfjs-dist/build/pdf.worker.min.mjs',
    import.meta.url
  ).toString();
  initialized = true;
}
