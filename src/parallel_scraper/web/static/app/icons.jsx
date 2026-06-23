// icon set — minimal stroke icons for parallel-scraper
// All icons are 16×16, currentColor, 1.5 stroke width.

const Ic = ({ d, fill, viewBox = "0 0 16 16", size = 14, sw = 1.5, ...rest }) => (
  <svg width={size} height={size} viewBox={viewBox} fill={fill ? "currentColor" : "none"}
       stroke="currentColor" strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round" {...rest}>
    {typeof d === "string" ? <path d={d} /> : d}
  </svg>
);

const Icons = {
  // brand mark — 4 squares, top-right detached (suggests parallel work + grid scraping)
  Brand: ({ size = 22 }) => (
    <svg width={size} height={size} viewBox="0 0 22 22" fill="none">
      <rect x="2"  y="2"  width="8" height="8" rx="1.5" fill="var(--accent)" />
      <rect x="12" y="2"  width="8" height="8" rx="1.5" fill="var(--accent)" opacity="0.55" />
      <rect x="2"  y="12" width="8" height="8" rx="1.5" fill="var(--accent)" opacity="0.7"/>
      <rect x="12" y="12" width="8" height="8" rx="1.5" fill="var(--accent)" opacity="0.3"/>
    </svg>
  ),

  Plus: (p) => <Ic d="M8 3.5v9 M3.5 8h9" {...p} />,
  Search: (p) => <Ic d={<><circle cx="7" cy="7" r="4.5"/><path d="m13.5 13.5-3-3"/></>} {...p} />,
  ListSquare: (p) => <Ic d={<><rect x="2" y="2" width="12" height="12" rx="2"/><path d="M5 6h6 M5 8h6 M5 10h4"/></>} {...p} />,
  Refresh: (p) => <Ic d="M2.5 8a5.5 5.5 0 0 1 9.5-3.8 M13.5 8a5.5 5.5 0 0 1-9.5 3.8 M12 1.5v3h-3 M4 14.5v-3h3" {...p} />,
  Play: (p) => <Ic d="M4 3.5v9l8-4.5z" {...p} />,
  Pause: (p) => <Ic d="M5 3v10 M11 3v10" {...p} />,
  Stop: (p) => <Ic d="M4 4h8v8H4z" {...p} />,
  Square: (p) => <Ic d={<><rect x="2.5" y="2.5" width="11" height="11" rx="1"/></>} {...p} />,
  Hex: (p) => <Ic d="M8 1.5l5.5 3v6L8 13.5 2.5 10.5v-6z" {...p} />,
  Map: (p) => <Ic d="M2 4l4-1.5 4 1.5 4-1.5v9.5l-4 1.5-4-1.5-4 1.5z M6 2.5v11 M10 4v11" {...p} />,
  Pin: (p) => <Ic d="M8 14s5-4 5-8a5 5 0 0 0-10 0c0 4 5 8 5 8z M8 6.5v.01" {...p} />,
  Globe: (p) => <Ic d={<><circle cx="8" cy="8" r="6"/><path d="M2 8h12 M8 2c2 2 2 10 0 12 M8 2c-2 2-2 10 0 12"/></>} {...p} />,
  Boundary: (p) => <Ic d={<><path d="M3 5l3-2 4 2 3-1v8l-3 1-4-2-3 2z"/></>} {...p} />,
  Pen: (p) => <Ic d="M2 14l1-3 8-8 2 2-8 8-3 1z" {...p} />,
  Database: (p) => <Ic d={<><ellipse cx="8" cy="3.5" rx="5.5" ry="1.8"/><path d="M2.5 3.5v9c0 1 2.5 1.8 5.5 1.8s5.5-.8 5.5-1.8v-9 M2.5 8c0 1 2.5 1.8 5.5 1.8s5.5-.8 5.5-1.8"/></>} {...p} />,
  Cpu: (p) => <Ic d={<><rect x="3" y="3" width="10" height="10" rx="1.5"/><rect x="6" y="6" width="4" height="4"/><path d="M6 1v2 M10 1v2 M6 13v2 M10 13v2 M1 6h2 M1 10h2 M13 6h2 M13 10h2"/></>} {...p} />,
  Layers: (p) => <Ic d="M8 1.5L1.5 5 8 8.5 14.5 5z M1.5 8L8 11.5 14.5 8 M1.5 11L8 14.5 14.5 11" {...p} />,
  X: (p) => <Ic d="M3.5 3.5l9 9 M12.5 3.5l-9 9" {...p} />,
  Check: (p) => <Ic d="M3 8l3.5 3.5L13 4.5" {...p} />,
  ChevronRight: (p) => <Ic d="M6 3l5 5-5 5" {...p} />,
  ChevronLeft: (p) => <Ic d="M10 3L5 8l5 5" {...p} />,
  ChevronDown: (p) => <Ic d="M3 6l5 5 5-5" {...p} />,
  ArrowRight: (p) => <Ic d="M3 8h10 M9 4l4 4-4 4" {...p} />,
  Filter: (p) => <Ic d="M2 3h12l-4.5 6v4l-3 1.5V9z" {...p} />,
  Download: (p) => <Ic d="M8 1.5v9 M4 7.5l4 4 4-4 M2 14h12" {...p} />,
  Settings: (p) => <Ic d={<><circle cx="8" cy="8" r="2"/><path d="M8 1.5v2 M8 12.5v2 M14.5 8h-2 M3.5 8h-2 M12.6 3.4l-1.4 1.4 M4.8 11.2l-1.4 1.4 M12.6 12.6l-1.4-1.4 M4.8 4.8L3.4 3.4"/></>} {...p} />,
  Clock: (p) => <Ic d={<><circle cx="8" cy="8" r="6"/><path d="M8 5v3l2 2"/></>} {...p} />,
  Activity: (p) => <Ic d="M1 8h3l2-5 4 10 2-5h3" {...p} />,
  Bolt: (p) => <Ic d="M9 1.5L3 9h4l-1 5.5L13 7H9z" {...p} />,
  Trash: (p) => <Ic d="M3 4h10 M5.5 4V2.5h5V4 M4.5 4l1 10h5l1-10 M7 7v5 M9 7v5" {...p} />,
  Save: (p) => <Ic d="M3 2h8l2 2v10H3z M5 2v4h6V2 M5 9h6" {...p} />,
  External: (p) => <Ic d="M9 2.5h4.5V7 M13.5 2.5l-7 7 M6.5 4H3v9h9V9.5" {...p} />,
  Phone: (p) => <Ic d="M3 2.5h3l1 3-2 1c1 2 3 4 5 5l1-2 3 1v3c0 .5-.5 1-1 1A12 12 0 0 1 2 3.5c0-.5.5-1 1-1z" {...p} />,
  Mail: (p) => <Ic d={<><rect x="2" y="3.5" width="12" height="9" rx="1.5"/><path d="M2.5 4.5l5.5 4 5.5-4"/></>} {...p} />,
  Star: (p) => <Ic d="M8 1.5l2 4.5 5 .5-3.7 3.4 1.1 5L8 12.5l-4.4 2.4 1.1-5L1 6.5l5-.5z" {...p} />,
  ScanLine: (p) => <Ic d="M2 5V3h2 M14 5V3h-2 M2 11v2h2 M14 11v2h-2 M2 8h12" {...p} />,
  Sun: (p) => <Ic d={<><circle cx="8" cy="8" r="3"/><path d="M8 1v2 M8 13v2 M1 8h2 M13 8h2 M3 3l1.5 1.5 M11.5 11.5L13 13 M3 13l1.5-1.5 M11.5 4.5L13 3"/></>} {...p} />,
  Moon: (p) => <Ic d="M13 9.5A6 6 0 0 1 6.5 3 6 6 0 1 0 13 9.5z" {...p} />,
  Eye: (p) => <Ic d={<><path d="M1 8s2.5-4.5 7-4.5S15 8 15 8s-2.5 4.5-7 4.5S1 8 1 8z"/><circle cx="8" cy="8" r="2"/></>} {...p} />,
  Sliders: (p) => <Ic d="M3 4h7 M12 4h1 M3 8h1 M6 8h7 M3 12h5 M10 12h3 M11 2.5v3 M5 6.5v3 M9 10.5v3" {...p} />,
  Layout: (p) => <Ic d={<><rect x="2" y="2" width="12" height="12" rx="1"/><path d="M2 6h12 M6 6v8"/></>} {...p} />,
  Folder: (p) => <Ic d="M2 4.5c0-.5.5-1 1-1h3l1.5 1.5H13c.5 0 1 .5 1 1v6c0 .5-.5 1-1 1H3c-.5 0-1-.5-1-1z" {...p} />,
};

window.Icons = Icons;
