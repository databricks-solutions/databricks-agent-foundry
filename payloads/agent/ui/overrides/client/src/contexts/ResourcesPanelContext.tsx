import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

const STORAGE_KEY = 'resources-panel:state';

interface ResourcesPanelContextType {
  open: boolean;
  setOpen: (open: boolean) => void;
  toggle: () => void;
}

const ResourcesPanelContext = createContext<
  ResourcesPanelContextType | undefined
>(undefined);

/** Agent Bricks: open state of the Unity Gateway resources panel. */
export function ResourcesPanelProvider({ children }: { children: ReactNode }) {
  const [open, setOpenState] = useState(
    () => localStorage.getItem(STORAGE_KEY) === 'true',
  );

  const setOpen = useCallback((next: boolean) => {
    localStorage.setItem(STORAGE_KEY, String(next));
    setOpenState(next);
  }, []);

  const toggle = useCallback(() => setOpen(!open), [open, setOpen]);
  const value = useMemo(
    () => ({ open, setOpen, toggle }),
    [open, setOpen, toggle],
  );

  return (
    <ResourcesPanelContext.Provider value={value}>
      {children}
    </ResourcesPanelContext.Provider>
  );
}

export function useResourcesPanel(): ResourcesPanelContextType {
  const context = useContext(ResourcesPanelContext);
  if (context === undefined) {
    throw new Error(
      'useResourcesPanel must be used within a ResourcesPanelProvider',
    );
  }
  return context;
}
