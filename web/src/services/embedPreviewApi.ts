import axios from 'axios';
import type { EmbedDocumentPreviewResponse } from '../types';

const runtimeApiBaseUrl = window.__OPENRAG_CONFIG__?.apiBaseUrl?.trim();
const envApiBaseUrl = import.meta.env.VITE_API_URL?.trim();
const resolvedApiBaseUrl = runtimeApiBaseUrl || envApiBaseUrl || '/api';

const embedPreviewClient = axios.create({
  baseURL: resolvedApiBaseUrl,
});

function previewTokenConfig(token: string) {
  return {
    headers: {
      'X-OpenRag-Preview-Token': token,
    },
  };
}

export function readPreviewTokenFromHash(hash = window.location.hash): string | null {
  const normalized = hash.startsWith('#') ? hash.slice(1) : hash;
  const token = new URLSearchParams(normalized).get('token')?.trim();
  return token || null;
}

export const embedPreviewAPI = {
  getContext: async (token: string): Promise<EmbedDocumentPreviewResponse> => {
    const response = await embedPreviewClient.get('/embed/v1/document-preview', previewTokenConfig(token));
    return response.data as EmbedDocumentPreviewResponse;
  },
  fetchContentBlob: async (token: string): Promise<Blob> => {
    const response = await embedPreviewClient.get('/embed/v1/files/content', {
      ...previewTokenConfig(token),
      responseType: 'blob',
    });
    return response.data as Blob;
  },
  fetchPreview: async (token: string): Promise<{ format: 'html' | 'text'; content: string }> => {
    const response = await embedPreviewClient.get('/embed/v1/files/preview', previewTokenConfig(token));
    return response.data as { format: 'html' | 'text'; content: string };
  },
  fetchChunkSource: async (token: string): Promise<{ format: 'text'; content: string }> => {
    const response = await embedPreviewClient.get('/embed/v1/files/chunk-source', previewTokenConfig(token));
    return response.data as { format: 'text'; content: string };
  },
};
