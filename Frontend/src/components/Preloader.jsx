import React from 'react';
import preloaderIcon from '../asset/preloader.svg';

export default function Preloader({ show }) {
  return (
    <div
      className={`fixed inset-0 z-[99999] flex flex-col items-center justify-center bg-white transition-opacity duration-1000 ease-[cubic-bezier(0.22,1,0.36,1)] ${
        show ? 'opacity-100 pointer-events-auto' : 'opacity-0 pointer-events-none'
      }`}
    >
      <div className="relative flex h-32 w-32 items-center justify-center">
        <img 
          src={preloaderIcon} 
          alt="Loading EventFlow AI..." 
          className="h-full w-full object-contain"
        />
      </div>

      <div className="mt-8 flex flex-col items-center gap-2">
        <div className="text-xs font-bold tracking-[0.4em] text-slate-800 uppercase">
          EventFlow AI
        </div>
        <div className="text-[10px] font-medium tracking-widest text-slate-500 uppercase">
          Initializing Systems
        </div>
      </div>
    </div>
  );
}
