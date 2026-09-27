import { Menu, X } from 'lucide-react';
import { useStore } from '../store/useStore.js';

export default function MenuToggle({ className = '' }) {
  const menuOpen = useStore((s) => s.menuOpen);
  const setMenuOpen = useStore((s) => s.setMenuOpen);

  return (
    <button
      onClick={() => setMenuOpen(!menuOpen)}
      className={`relative flex h-8 w-8 shrink-0 items-center justify-center rounded bg-surface-800 border border-surface-700 text-slate-200 shadow-sm transition-transform duration-300 hover:scale-105 hover:bg-surface-700 focus:outline-none ${className}`}
      aria-label="Toggle Navigation Menu"
    >
      <div className="relative h-4 w-4">
        <Menu 
          className={`absolute inset-0 h-4 w-4 transition-all duration-300 ${
            menuOpen ? 'rotate-90 scale-0 opacity-0' : 'rotate-0 scale-100 opacity-100'
          }`} 
        />
        <X 
          className={`absolute inset-0 h-4 w-4 transition-all duration-300 ${
            menuOpen ? 'rotate-0 scale-100 opacity-100' : '-rotate-90 scale-0 opacity-0'
          }`} 
        />
      </div>
    </button>
  );
}
