import { create } from 'zustand';
import type { Dataset, ImageRecord } from './types';

type View = 'explore' | 'jobs' | 'scaling';
interface AppState {
  activeView: View;
  selectedDataset: Dataset | null;
  selectedImage: ImageRecord | null;
  setView: (view: View) => void;
  setDataset: (dataset: Dataset | null) => void;
  setImage: (image: ImageRecord | null) => void;
}

export const useAppStore = create<AppState>((set) => ({
  activeView: 'explore',
  selectedDataset: null,
  selectedImage: null,
  setView: (activeView) => set({ activeView }),
  setDataset: (selectedDataset) => set({ selectedDataset, selectedImage: null }),
  setImage: (selectedImage) => set({ selectedImage }),
}));
