/**
 * The product's navigation, in order — one source of truth shared by the
 * horizontal NavBar and the NavShell menu. Routes and labels are unchanged
 * from the original NavBar; anything rendering navigation reads this list.
 */
import {
  LayoutDashboard,
  CalendarClock,
  BedDouble,
  TrainFront,
  Users,
  ListChecks,
  GitBranch,
  Smartphone,
  MessageSquare,
  BarChart3,
  MapPin,
  CloudRain,
} from 'lucide-react';

export const NAV_ITEMS = [
  { to: '/', label: 'Command Centre', icon: LayoutDashboard, end: true, hint: 'Live network map' },
  { to: '/venue', label: 'Venue & Network', icon: MapPin, hint: 'Build a world from OSM' },
  { to: '/events', label: 'Events', icon: CalendarClock, hint: 'Schedule and phases' },
  { to: '/accommodation', label: 'Hotels', icon: BedDouble, hint: 'Occupancy and recommendations' },
  { to: '/transport', label: 'Transport', icon: TrainFront, hint: 'Stations, hubs, parking' },
  { to: '/crowd', label: 'Crowd & Venues', icon: Users, hint: 'Gates, queues, egress' },
  { to: '/interventions', label: 'Interventions', icon: ListChecks, hint: 'Certified action queue' },
  { to: '/whatif', label: 'What-If', icon: GitBranch, hint: 'Counterfactual simulation' },
  { to: '/twin', label: 'Digital Twin', icon: CloudRain, hint: 'Weather-driven twin state' },
  { to: '/attendee', label: 'Attendee', icon: Smartphone, hint: 'Attendee-side PWA' },
  { to: '/commander', label: 'Commander', icon: MessageSquare, hint: 'Natural-language ops' },
  { to: '/metrics', label: 'Metrics', icon: BarChart3, hint: 'Measured outcomes' },
];
