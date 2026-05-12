import { useState, useCallback } from 'react';
import type { ChunkInfo } from '../../utils/chunk-context-util';

export interface UseChunkContextModalResult {
  isOpen: boolean;
  selectedChunk: ChunkInfo | null;
  openModal: (chunk: ChunkInfo) => void;
  closeModal: () => void;
}

export function useChunkContextModal(): UseChunkContextModalResult {
  const [isOpen, setIsOpen] = useState(false);
  const [selectedChunk, setSelectedChunk] = useState<ChunkInfo | null>(null);

  const openModal = useCallback((chunk: ChunkInfo) => {
    setSelectedChunk(chunk);
    setIsOpen(true);
  }, []);

  const closeModal = useCallback(() => {
    setIsOpen(false);
    // Delay clearing chunk to allow exit animation
    setTimeout(() => setSelectedChunk(null), 300);
  }, []);

  return {
    isOpen,
    selectedChunk,
    openModal,
    closeModal,
  };
}
