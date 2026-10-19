import React from 'react';
import CarbonLookup from './components/CarbonLookup';
import './index.css';

export default function App() {
  return (
    <div className="app-container">
      <div className="hero card">
        <div className="leaf">🌿</div>
        <div>
          <h1> CARBON STOCK ESTIMATION</h1>
          <p>Paste latitude and longitude to compute area and carbon totals for a 2.5 km buffer.</p>
        </div>
      </div>

      <div style={{height:16}} />

      <div className="card">
        <CarbonLookup />
      </div>
    </div>
  );
}
