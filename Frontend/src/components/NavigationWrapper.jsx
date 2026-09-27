import { useEffect } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import { LayoutDashboard, CalendarClock, BedDouble, TrainFront, Users, ListChecks, GitBranch, Smartphone, MessageSquare, BarChart3, MapPin } from 'lucide-react';
import { useStore } from '../store/useStore.js';
import FloatingSimControls from './FloatingSimControls.jsx';

const LINKS = [
  { to: '/', label: 'Command Centre', icon: LayoutDashboard, end: true },
  { to: '/venue', label: 'Venue & Network', icon: MapPin },
  { to: '/events', label: 'Events', icon: CalendarClock },
  { to: '/accommodation', label: 'Hotels', icon: BedDouble },
  { to: '/transport', label: 'Transport', icon: TrainFront },
  { to: '/crowd', label: 'Crowd & Venues', icon: Users },
  { to: '/interventions', label: 'Interventions', icon: ListChecks },
  { to: '/whatif', label: 'What-If', icon: GitBranch },
  { to: '/attendee', label: 'Attendee', icon: Smartphone },
  { to: '/commander', label: 'Commander', icon: MessageSquare },
  { to: '/metrics', label: 'Metrics', icon: BarChart3 },
];

export default function NavigationWrapper({ children }) {
  const menuOpen = useStore((s) => s.menuOpen);
  const setMenuOpen = useStore((s) => s.setMenuOpen);
  const location = useLocation();

  // Close menu on route change
  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname, setMenuOpen]);

  return (
    <div className="relative h-screen w-screen bg-surface-900 overflow-hidden font-sans flex text-slate-200 perspective-[2000px]">
      {/* LEFT MENU AREA */}
      <div className="absolute inset-y-0 left-0 w-[60%] md:w-80 p-6 flex flex-col justify-center z-0">
        <div className="mb-8 px-4">
          <h2 className="text-xl font-bold tracking-tight text-slate-100">EventFlow AI</h2>
          <p className="text-sm text-slate-400">Main Menu</p>
        </div>
        <nav className="flex flex-col gap-1 overflow-y-auto pr-2 pb-4">
          {LINKS.map(({ to, label, icon: Icon, end }, i) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              onClick={() => setMenuOpen(false)}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-xl px-4 py-3 text-sm font-medium transition-all duration-300 ${
                  isActive
                    ? 'bg-teal-500/20 text-teal-400 shadow-[0_0_15px_rgba(20,184,166,0.1)]'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-surface-800'
                }`
              }
              style={{
                transitionDelay: `${menuOpen ? i * 30 : 0}ms`,
                opacity: menuOpen ? 1 : 0,
                transform: menuOpen ? 'translateX(0)' : 'translateX(-20px)',
              }}
            >
              <Icon className="h-5 w-5" />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
      </div>

      {/* MAIN CONTENT TRANSFORM */}
      <div
        className={`relative z-10 h-full w-full flex-1 overflow-hidden bg-surface-950 transition-all duration-500 ease-[cubic-bezier(0.32,0.72,0,1)] flex flex-col ${
          menuOpen
            ? 'scale-[0.85] translate-x-[60%] md:translate-x-80 rounded-[2rem] shadow-[-20px_0_50px_rgba(0,0,0,0.5)] cursor-pointer ring-1 ring-surface-700/50'
            : 'scale-100 translate-x-0 rounded-none cursor-auto'
        }`}
        style={{
          transformOrigin: 'center left',
          // Optional subtle 3D tilt effect:
          // transform: menuOpen ? 'scale(0.85) translateX(60%) rotateY(-5deg)' : 'none',
        }}
      >
        {/* Transparent overlay to capture clicks and close the menu when clicking on the transformed main page */}
        {menuOpen && (
          <div
            className="absolute inset-0 z-[9999] bg-black/10 backdrop-blur-[1px] transition-opacity duration-500 rounded-[2rem]"
            onClick={() => setMenuOpen(false)}
          />
        )}

        <div className="h-full w-full pointer-events-auto flex flex-col flex-1 min-h-0 relative">
          {children}
          <FloatingSimControls />
        </div>
      </div>
    </div>
  );
}
