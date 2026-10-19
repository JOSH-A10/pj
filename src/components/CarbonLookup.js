import React, {useState} from 'react';

export default function CarbonLookup(){
  const [lat, setLat] = useState('');
  const [lon, setLon] = useState('');
  const DEFAULT_RADIUS_M = 2500; // fixed buffer used for area calculations
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const submit = async (e) =>{
    e.preventDefault();
    setError(null);
    setResult(null);
    const plat = parseFloat(lat);
    const plon = parseFloat(lon);
    if (Number.isNaN(plat) || Number.isNaN(plon)){
      setError('Invalid lat or lon');
      return;
    }
    const pr = DEFAULT_RADIUS_M;
    setLoading(true);
    try{
      const url = `http://localhost:5000/api/carbon?lat=${encodeURIComponent(plat)}&lon=${encodeURIComponent(plon)}`;
      const res = await fetch(url);
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      const data = await res.json();
      setResult({...data, _radius_m: pr});
    }catch(err){
      setError(err.message);
    }finally{
      setLoading(false);
    }
  }

  return (
    <div style={{maxWidth:480}}>
      <form onSubmit={submit}>
        <div className="form-row">
          <div style={{flex:1}}>
            <label className="meta">Latitude</label>
            <input value={lat} onChange={e=>setLat(e.target.value)} placeholder="e.g. 12.5" />
          </div>
          <div style={{flex:1}}>
            <label className="meta">Longitude</label>
            <input value={lon} onChange={e=>setLon(e.target.value)} placeholder="e.g. 75.75" />
          </div>
        </div>

        <div style={{display:'flex',gap:12,alignItems:'center'}}>
          <button className="btn" type="submit" disabled={loading}>{loading? 'Loading...':'Lookup'}</button>
          <div className="meta">Buffer: 2.5 km • Area auto-calculated</div>
        </div>
      </form>

      {error && <div style={{color:'red',marginTop:12}}>Error: {error}</div>}

      <div style={{height:8}} />

      <div className={`result ${result? 'show':''}`} aria-live="polite">
        {result && (
          <div className="card">
            <div className="result-row"><div className="meta">Entered</div><div>{result.input_lat}, {result.input_lon}</div></div>
            <div className="result-row"><div className="meta">Region</div><div>{result.region || '—'}</div></div>
            <div className="result-row"><div className="meta">Area (ha)</div><div>{(() => {const r = Number(result._radius_m || DEFAULT_RADIUS_M);const area_m2 = Math.PI * r * r;const area_ha = area_m2 / 10000;return area_ha.toFixed(4);})()}</div></div>
            <div className="result-row"><div className="meta">Avg Carbon (Mg C/ha)</div><div>{result.avg_carbon_mg_per_ha ?? 'N/A'}</div></div>
            <div className="result-row"><div className="meta">Total Carbon (Mg C)</div><div>{(() => {const avg = Number(result.avg_carbon_mg_per_ha);const r = Number(result._radius_m || DEFAULT_RADIUS_M);if (!isFinite(avg) || isNaN(avg)) return 'N/A';const area_ha = (Math.PI * r * r) / 10000;const total_carbon = avg * area_ha;return total_carbon.toFixed(3);})()}</div></div>
          </div>
        )}
      </div>
    </div>
  )
}
