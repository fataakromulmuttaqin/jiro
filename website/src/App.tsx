import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { Sidebar } from './components/Sidebar';
import { Overview } from './pages/Overview';
import { Wallets } from './pages/Wallets';
import { Cabals } from './pages/Cabals';
import { Watchlist } from './pages/Watchlist';
import { Behavior } from './pages/Behavior';
import { MintDetail } from './pages/MintDetail';

function App() {
  return (
    <BrowserRouter>
      <div className="flex min-h-screen bg-bg-primary">
        <Sidebar />
        <main className="flex-1 overflow-x-hidden">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/wallets" element={<Wallets />} />
            <Route path="/cabals" element={<Cabals />} />
            <Route path="/watchlist" element={<Watchlist />} />
            <Route path="/behavior" element={<Behavior />} />
            <Route path="/mint/:mintShort" element={<MintDetail />} />
            <Route path="*" element={
              <div className="p-8 font-mono text-sm text-steel">
                404 — page not found
              </div>
            } />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}

export default App;