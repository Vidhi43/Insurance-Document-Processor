import { useState, useRef, useEffect } from 'react';
import { UploadCloud, FileText, CheckCircle2, AlertTriangle, Loader2,
  Copy, Download, Edit, RefreshCw, FileJson, LayoutGrid,
  BarChart2, TrendingUp, Zap } from 'lucide-react';

const API_BASE_URL = 'http://127.0.0.1:8000';

const STEPS = [
  { id: 1, title: 'Document Upload',    desc: 'Saving uploaded file to server...' },
  { id: 2, title: 'Data Extraction',    desc: 'Running OCR & layout-aware text extraction...' },
  { id: 3, title: 'Classify & Extract', desc: 'Classifying type, querying LLM for fields...' },
  { id: 4, title: 'Validation',         desc: 'Validating extracted fields against schema...' },
];

const COMPARISON_MODELS = [
  {
    name: 'Gemma 4 31B',
    color: '#a855f7',
    overall: { exact: 80.69, similarity: 95.77, fields: 321 },
    doc_types: {
      pdf:   { exact: 86.57, similarity: 97.26 },
      word:  { exact: 90.00, similarity: 95.58 },
      image: { exact: 60.42, similarity: 96.37 },
      video: { exact: 64.29, similarity: 88.27 },
      audio: { exact: 53.85, similarity: 77.24 }
    },
    latency: 2.4, input_tokens: 2400, output_tokens: 350
  },
  {
    name: 'Llama 3.3 70B',
    color: '#3b82f6',
    overall: { exact: 79.76, similarity: 93.90, fields: 252 },
    doc_types: {
      pdf:   { exact: 86.39, similarity: 95.25 },
      word:  { exact: 86.67, similarity: 93.98 },
      image: { exact: 66.67, similarity: 96.75 },
      video: { exact: 64.29, similarity: 89.04 },
      audio: { exact: 53.85, similarity: 73.14 }
    },
    latency: 1.8, input_tokens: 2300, output_tokens: 320
  },
  {
    name: 'Qwen 3 32B',
    color: '#10b981',
    overall: { exact: 75.61, similarity: 92.43, fields: 164 },
    doc_types: {
      pdf:   { exact: 84.48, similarity: 94.57 },
      word:  { exact: 83.87, similarity: 93.47 },
      image: { exact: 64.58, similarity: 96.64 },
      video: { exact: 78.57, similarity: 90.89 },
      audio: { exact: 53.85, similarity: 66.49 }
    },
    latency: 2.1, input_tokens: 2500, output_tokens: 380
  }
];

const FIELD_GROUPS = {
  life: [
    { title: 'Insured Information', fields: [
      { key: 'insured_name',     label: 'Insured Name',    placeholder: 'e.g. Mr J Patil' },
      { key: 'date_of_birth',    label: 'Date of Birth',   placeholder: 'DD/MM/YYYY' },
      { key: 'occupation',       label: 'Occupation',      placeholder: 'e.g. AI/ML Engineer' },
      { key: 'cell_phone',       label: 'Cell Phone',      placeholder: 'e.g. 9782793478' },
      { key: 'email',            label: 'Email',           placeholder: 'e.g. name@domain.com' },
      { key: 'co_insured_name',  label: 'Co-Insured',      placeholder: '' },
      { key: 'physical_address', label: 'Physical Address',placeholder: '', textarea: true },
      { key: 'postal_address',   label: 'Postal Address',  placeholder: '', textarea: true },
    ]},
    { title: 'Policy Details', fields: [
      { key: 'policy_number_broker',    label: 'Policy Number',  placeholder: '' },
      { key: 'policy_type',             label: 'Policy Type',    placeholder: '' },
      { key: 'policy_status',           label: 'Status',         placeholder: '' },
      { key: 'start_date_of_cover',     label: 'Cover Start',    placeholder: 'DD/MM/YYYY' },
      { key: 'anniversary_date',        label: 'Anniversary',    placeholder: 'DD/MM/YYYY' },
      { key: 'original_inception_date', label: 'Inception Date', placeholder: 'DD/MM/YYYY' },
      { key: 'total_premium',           label: 'Total Premium',  placeholder: '' },
      { key: 'payment_method',          label: 'Payment Method', placeholder: '' },
      { key: 'period_of_insurance',     label: 'Period',         placeholder: '', textarea: true },
    ]},
    { title: 'Insurer & Broker', fields: [
      { key: 'insurer_name',      label: 'Insurer',    placeholder: '' },
      { key: 'insurer_phone',     label: 'Insurer Ph', placeholder: '' },
      { key: 'intermediary_name', label: 'Broker',     placeholder: '' },
    ]},
  ],
  car: [
    { title: 'Proposer Details', fields: [
      { key: 'proposer_name',    label: 'Name',       placeholder: '' },
      { key: 'proposer_mobile',  label: 'Mobile',     placeholder: '' },
      { key: 'proposer_email',   label: 'Email',      placeholder: '' },
      { key: 'customer_id',      label: 'Customer ID',placeholder: '' },
      { key: 'proposer_address', label: 'Address',    placeholder: '', textarea: true },
    ]},
    { title: 'Vehicle Details', fields: [
      { key: 'registration_number', label: 'Reg Number',  placeholder: '' },
      { key: 'vehicle_make',        label: 'Make',        placeholder: '' },
      { key: 'vehicle_model',       label: 'Model',       placeholder: '' },
      { key: 'vehicle_sub_type',    label: 'Sub Type',    placeholder: '' },
      { key: 'year_of_manufacture', label: 'Year',        placeholder: '' },
      { key: 'engine_number',       label: 'Engine No',   placeholder: '' },
      { key: 'chassis_number',      label: 'Chassis No',  placeholder: '' },
      { key: 'vehicle_idv',         label: 'Vehicle IDV', placeholder: '' },
      { key: 'total_idv',           label: 'Total IDV',   placeholder: '' },
      { key: 'ncb_percent',         label: 'NCB %',       placeholder: '' },
    ]},
    { title: 'Policy & Premium', fields: [
      { key: 'policy_number',          label: 'Policy No',       placeholder: '' },
      { key: 'previous_policy_number', label: 'Previous Policy', placeholder: '' },
      { key: 'policy_period_start',    label: 'Period Start',    placeholder: '' },
      { key: 'policy_period_end',      label: 'Period End',      placeholder: '' },
      { key: 'own_damage_premium',     label: 'OD Premium',      placeholder: '' },
      { key: 'liability_premium',      label: 'Liability Prem',  placeholder: '' },
      { key: 'total_premium',          label: 'Net Premium',     placeholder: '' },
      { key: 'final_premium',          label: 'Final Premium',   placeholder: '' },
      { key: 'broker_name',            label: 'Broker',          placeholder: '' },
      { key: 'broker_code',            label: 'Broker Code',     placeholder: '' },
    ]},
  ],
  travel: [
    { title: 'Certificate & Policy', fields: [
      { key: 'master_policy_number',    label: 'Master Policy No',  placeholder: '' },
      { key: 'certificate_number',      label: 'Certificate No',    placeholder: '' },
      { key: 'group_policyholder_name', label: 'Group Policyholder',placeholder: '' },
      { key: 'insurer_name',            label: 'Insurer',           placeholder: '' },
      { key: 'certificate_issue_date',  label: 'Issue Date',        placeholder: '' },
      { key: 'gst_number',              label: 'GST No',            placeholder: '' },
      { key: 'pan_number',              label: 'PAN No',            placeholder: '' },
    ]},
    { title: 'Insured & Trip', fields: [
      { key: 'insured_name',         label: 'Insured Name', placeholder: '' },
      { key: 'insured_age',          label: 'Age',          placeholder: '' },
      { key: 'insured_gender',       label: 'Gender',       placeholder: '' },
      { key: 'insured_mobile',       label: 'Mobile',       placeholder: '' },
      { key: 'insured_email',        label: 'Email',        placeholder: '' },
      { key: 'pnr_number',           label: 'PNR',          placeholder: '' },
      { key: 'train_number',         label: 'Train No',     placeholder: '' },
      { key: 'train_name',           label: 'Train Name',   placeholder: '' },
      { key: 'originating_station',  label: 'From Station', placeholder: '' },
      { key: 'destination_station',  label: 'To Station',   placeholder: '' },
      { key: 'trip_start',           label: 'Trip Start',   placeholder: '' },
      { key: 'trip_end',             label: 'Trip End',     placeholder: '' },
    ]},
    { title: 'Coverage & Premium', fields: [
      { key: 'cover_death',                        label: 'Death Cover',    placeholder: '' },
      { key: 'cover_permanent_total_disability',   label: 'PTD Cover',      placeholder: '' },
      { key: 'cover_permanent_partial_disability', label: 'PPD Cover',      placeholder: '' },
      { key: 'cover_hospitalization_expenses',     label: 'Hospitalization',placeholder: '' },
      { key: 'cover_transportation_mortal_remains',label: 'Mortal Remains', placeholder: '' },
      { key: 'base_premium',                       label: 'Base Premium',   placeholder: '' },
      { key: 'igst',                               label: 'IGST',           placeholder: '' },
      { key: 'total_premium',                      label: 'Total Premium',  placeholder: '' },
    ]},
  ],
  health: [
    { title: 'Policy & Insurer', fields: [
      { key: 'policy_number',     label: 'Policy No',      placeholder: '' },
      { key: 'insurer_name',      label: 'Insurer',        placeholder: '' },
      { key: 'customer_code',     label: 'Customer Code',  placeholder: '' },
      { key: 'gstin',             label: 'GSTIN',          placeholder: '' },
      { key: 'issuing_office',    label: 'Issuing Office', placeholder: '' },
      { key: 'date_of_inception', label: 'Inception Date', placeholder: '' },
      { key: 'period_start',      label: 'Period Start',   placeholder: '' },
      { key: 'period_end',        label: 'Period End',     placeholder: '' },
    ]},
    { title: 'Proposer Details', fields: [
      { key: 'proposer_name',    label: 'Proposer Name', placeholder: '' },
      { key: 'proposer_mobile',  label: 'Mobile',        placeholder: '' },
      { key: 'proposer_email',   label: 'Email',         placeholder: '' },
      { key: 'proposer_address', label: 'Address',       placeholder: '', textarea: true },
    ]},
    { title: 'Coverage & Premium', fields: [
      { key: 'sum_insured',        label: 'Sum Insured',       placeholder: '' },
      { key: 'scheme_description', label: 'Scheme',            placeholder: '' },
      { key: 'base_premium',       label: 'Premium',           placeholder: '' },
      { key: 'stamp_duty',         label: 'Stamp Duty',        placeholder: '' },
      { key: 'total_premium',      label: 'Total Premium',     placeholder: '' },
      { key: 'intermediary_code',  label: 'Intermediary Code', placeholder: '' },
      { key: 'intermediary_name',  label: 'Intermediary',      placeholder: '' },
      { key: 'nominee_name',       label: 'Nominee',           placeholder: '' },
      { key: 'insured_members',    label: 'Insured Members',   placeholder: '', textarea: true },
    ]},
  ],
  property: [
    { title: 'Policy & Insurer', fields: [
      { key: 'policy_number',        label: 'Policy No',       placeholder: '' },
      { key: 'insurer_name',         label: 'Insurer',         placeholder: '' },
      { key: 'endorsement_number',   label: 'Endorsement No',  placeholder: '' },
      { key: 'first_inception_date', label: 'First Inception', placeholder: '' },
      { key: 'effective_date',       label: 'Effective Date',  placeholder: '' },
      { key: 'anniversary_date',     label: 'Anniversary',     placeholder: '' },
      { key: 'master_policy_number', label: 'Master Policy',   placeholder: '' },
      { key: 'payment_frequency',    label: 'Payment Freq',    placeholder: '' },
      { key: 'narrative',            label: 'Narrative',       placeholder: '' },
    ]},
    { title: 'Insured Details', fields: [
      { key: 'insured_name',          label: 'Insured Name',       placeholder: '' },
      { key: 'insured_reg_number',    label: 'Reg No',             placeholder: '' },
      { key: 'insured_vat_number',    label: 'VAT No',             placeholder: '' },
      { key: 'postal_address',        label: 'Postal Address',     placeholder: '', textarea: true },
      { key: 'business_description',  label: 'Business',           placeholder: '', textarea: true },
      { key: 'territorial_limits',    label: 'Territorial Limits', placeholder: '', textarea: true },
      { key: 'period_of_insurance',   label: 'Period',             placeholder: '', textarea: true },
    ]},
    { title: 'Broker & Premium', fields: [
      { key: 'broker_name',       label: 'Broker',        placeholder: '' },
      { key: 'broker_vat_number', label: 'Broker VAT',    placeholder: '' },
      { key: 'broker_fsp_number', label: 'Broker FSP',    placeholder: '' },
      { key: 'total_premium',     label: 'Total Premium', placeholder: '' },
      { key: 'broker_fee',        label: 'Broker Fee',    placeholder: '' },
      { key: 'final_debit',       label: 'Final Debit',   placeholder: '' },
    ]},
  ],
};

function scoreColor(pct) {
  if (pct >= 80) return '#22c55e';
  if (pct >= 55) return '#f59e0b';
  return '#ef4444';
}

function scoreGlow(pct) {
  if (pct >= 80) return '0 0 12px rgba(34,197,94,0.45)';
  if (pct >= 55) return '0 0 12px rgba(245,158,11,0.45)';
  return '0 0 12px rgba(239,68,68,0.45)';
}

function Bar({ pct, delay = 0 }) {
  const [w, setW] = useState(0);
  useEffect(() => { const t = setTimeout(() => setW(pct), delay + 60); return () => clearTimeout(t); }, [pct, delay]);
  const color = scoreColor(pct);
  return (
    <div style={{ position: 'relative', height: 10, borderRadius: 6, background: 'rgba(255,255,255,0.06)', overflow: 'hidden' }}>
      <div style={{
        height: '100%', borderRadius: 6, width: `${w}%`,
        background: `linear-gradient(90deg, ${color}88, ${color})`,
        boxShadow: scoreGlow(pct),
        transition: `width 0.9s cubic-bezier(.4,0,.2,1) ${delay}ms`,
      }} />
    </div>
  );
}

function ComparisonPage() {
  const DOC_TYPES = ['pdf', 'word', 'image', 'video', 'audio'];
  const DOC_ICONS = { pdf:'📄', word:'📝', image:'🖼️', video:'🎬', audio:'🎙️' };

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:'2rem' }}>
      <div>
        <h2 style={{ margin:0, fontSize:'1.3rem', fontWeight:700 }}>Model Comparison</h2>
        <p style={{ margin:'0.25rem 0 0', fontSize:'0.8rem', color:'var(--text-muted)' }}>
          Benchmark results across 3 LLM providers
        </p>
      </div>

      <div className="card">
        <h3 style={{ margin:'0 0 1.25rem', fontSize:'0.85rem', textTransform:'uppercase', letterSpacing:'0.08em', color:'var(--text-muted)' }}>
          <Zap size={13} style={{ marginRight:6, verticalAlign:'middle' }} /> Overall Accuracy
        </h3>
        <div style={{ display:'grid', gridTemplateColumns:'repeat(auto-fit, minmax(250px, 1fr))', gap:'1.25rem' }}>
          {COMPARISON_MODELS.map(m => (
            <div key={m.name} style={{
              background:'rgba(255,255,255,0.03)',
              border:`1px solid ${m.color}33`,
              borderRadius:14,
              padding:'1.25rem',
              boxShadow:`0 0 20px ${m.color}11`
            }}>
              <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:'1rem' }}>
                <span style={{ fontWeight:700, fontSize:'1rem', color:m.color }}>{m.name}</span>
                <span style={{ fontSize:'0.7rem', color:'var(--text-muted)' }}>n={m.overall.fields}</span>
              </div>
              <div style={{ display:'flex', flexDirection:'column', gap:'0.75rem' }}>
                <div>
                  <div style={{ display:'flex', justifyContent:'space-between', fontSize:'0.75rem', marginBottom:4 }}>
                    <span>Exact Match</span>
                    <span style={{ fontWeight:600, color:scoreColor(m.overall.exact) }}>{m.overall.exact}%</span>
                  </div>
                  <Bar pct={m.overall.exact} />
                </div>
                <div>
                  <div style={{ display:'flex', justifyContent:'space-between', fontSize:'0.75rem', marginBottom:4 }}>
                    <span>Fuzzy Similarity</span>
                    <span style={{ fontWeight:600, color:scoreColor(m.overall.similarity) }}>{m.overall.similarity}%</span>
                  </div>
                  <Bar pct={m.overall.similarity} delay={100} />
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <h3 style={{ margin:'0 0 1.25rem', fontSize:'0.85rem', textTransform:'uppercase', letterSpacing:'0.08em', color:'var(--text-muted)' }}>
          <BarChart2 size={13} style={{ marginRight:6, verticalAlign:'middle' }} /> Document-Wise Exact Accuracy
        </h3>
        <div style={{ overflowX:'auto' }}>
          <table style={{ width:'100%', borderCollapse:'collapse', fontSize:'0.82rem' }}>
            <thead>
              <tr>
                <th style={{ textAlign:'left', padding:'0.75rem', color:'var(--text-muted)', borderBottom:'1px solid var(--border-color)', fontWeight:500 }}>Format</th>
                {COMPARISON_MODELS.map(m => (
                  <th key={m.name} style={{ textAlign:'center', padding:'0.75rem', color:m.color, borderBottom:'1px solid var(--border-color)', fontWeight:600 }}>{m.name}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {DOC_TYPES.map(dt => (
                <tr key={dt} style={{ borderBottom:'1px solid rgba(255,255,255,0.05)' }}>
                  <td style={{ padding:'0.85rem 0.75rem', fontWeight:500 }}>
                    <span style={{ marginRight:8 }}>{DOC_ICONS[dt]}</span> {dt.charAt(0).toUpperCase() + dt.slice(1)}
                  </td>
                  {COMPARISON_MODELS.map(m => {
                    const val = m.doc_types[dt].exact;
                    return (
                      <td key={m.name} style={{ padding:'0.85rem 0.75rem' }}>
                        <div style={{ display:'flex', flexDirection:'column', alignItems:'center', gap:4 }}>
                          <span style={{ fontWeight:700, color:scoreColor(val), fontSize:'0.9rem' }}>{val}%</span>
                          <div style={{ width:'100%', maxWidth:80, height:6, borderRadius:3, background:'rgba(255,255,255,0.06)', overflow:'hidden' }}>
                            <div style={{ height:'100%', width:`${val}%`, background:m.color, borderRadius:3, boxShadow:`0 0 6px ${m.color}88` }} />
                          </div>
                          <span style={{ fontSize:'0.65rem', color:'var(--text-muted)' }}>~{m.doc_types[dt].similarity}%</span>
                        </div>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <h3 style={{ margin:'0 0 1.25rem', fontSize:'0.85rem', textTransform:'uppercase', letterSpacing:'0.08em', color:'var(--text-muted)' }}>
          <TrendingUp size={13} style={{ marginRight:6, verticalAlign:'middle' }} /> Efficiency & Token Usage
        </h3>
        <div style={{ display:'grid', gridTemplateColumns:'repeat(auto-fit, minmax(250px, 1fr))', gap:'1.25rem' }}>
          {COMPARISON_MODELS.map(m => (
            <div key={m.name} style={{
              background:'rgba(255,255,255,0.03)',
              border:`1px solid ${m.color}33`,
              borderRadius:14,
              padding:'1.25rem'
            }}>
              <div style={{ fontWeight:700, fontSize:'1rem', color:m.color, marginBottom:'1rem' }}>{m.name}</div>
              <div style={{ display:'flex', flexDirection:'column', gap:'1rem' }}>
                <div>
                  <div style={{ display:'flex', justifyContent:'space-between', fontSize:'0.75rem', marginBottom:4 }}>
                    <span>Avg Latency</span>
                    <span style={{ fontWeight:600 }}>{m.latency}s</span>
                  </div>
                  <div style={{ height:6, borderRadius:3, background:'rgba(255,255,255,0.06)', overflow:'hidden' }}>
                    <div style={{ height:'100%', width:`${(m.latency / 3) * 100}%`, background:m.color, borderRadius:3 }} />
                  </div>
                </div>
                <div>
                  <div style={{ display:'flex', justifyContent:'space-between', fontSize:'0.75rem', marginBottom:4 }}>
                    <span>Avg Input Tokens</span>
                    <span style={{ fontWeight:600 }}>{m.input_tokens.toLocaleString()}</span>
                  </div>
                  <div style={{ height:6, borderRadius:3, background:'rgba(255,255,255,0.06)', overflow:'hidden' }}>
                    <div style={{ height:'100%', width:`${(m.input_tokens / 3000) * 100}%`, background:m.color, borderRadius:3 }} />
                  </div>
                </div>
                <div>
                  <div style={{ display:'flex', justifyContent:'space-between', fontSize:'0.75rem', marginBottom:4 }}>
                    <span>Avg Output Tokens</span>
                    <span style={{ fontWeight:600 }}>{m.output_tokens.toLocaleString()}</span>
                  </div>
                  <div style={{ height:6, borderRadius:3, background:'rgba(255,255,255,0.06)', overflow:'hidden' }}>
                    <div style={{ height:'100%', width:`${(m.output_tokens / 500) * 100}%`, background:m.color, borderRadius:3 }} />
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function GenericGroups({ data }) {
  return (
    <div className="card">
      <h2 className="card-title">Extracted Fields</h2>
      <div className="fields-grid">
        {Object.entries(data).map(([k, v]) => (
          <div key={k} className="field-group">
            <span className="field-label">{k.replace(/_/g, ' ')}</span>
            <input type="text" className="field-input" value={String(v ?? '')} disabled />
          </div>
        ))}
      </div>
    </div>
  );
}

export default function App() {
  const [page, setPage] = useState('process');
  const [file, setFile] = useState(null);
  const [dragActive, setDragActive] = useState(false);
  const [status, setStatus] = useState('idle');
  const [currentStep, setCurrentStep] = useState(0);
  const [stepMessage, setStepMessage] = useState('');
  const [result, setResult] = useState(null);
  const [docType, setDocType] = useState('');
  const [edited, setEdited] = useState(null);
  const [jsonText, setJsonText] = useState('');
  const [activeTab, setActiveTab] = useState('cards');
  const [editMode, setEditMode] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  const [isVideo, setIsVideo] = useState(false);
  const [extractionMode, setExtractionMode] = useState('both');
  const fileInputRef = useRef(null);

  const VIDEO_EXTS = ['.mp4','.mov','.avi','.mkv','.webm'];
  const VALID_EXTS = ['.pdf','.png','.jpg','.jpeg','.docx','.doc',
    '.mp3','.wav','.m4a','.aac','.flac',...VIDEO_EXTS];

  const validateAndSet = (f) => {
    const ext = f.name.substring(f.name.lastIndexOf('.')).toLowerCase();
    if (!VALID_EXTS.includes(ext)) { setErrorMsg(`Unsupported: ${ext}`); setStatus('error'); return; }
    setFile(f); setIsVideo(VIDEO_EXTS.includes(ext)); setStatus('idle'); setErrorMsg('');
  };

  const handleDrag = (e) => { e.preventDefault(); e.stopPropagation(); setDragActive(e.type !== 'dragleave'); };
  const handleDrop = (e) => { e.preventDefault(); e.stopPropagation(); setDragActive(false); if (e.dataTransfer.files[0]) validateAndSet(e.dataTransfer.files[0]); };

  const reset = () => {
    setFile(null); setResult(null); setEdited(null); setDocType('');
    setStatus('idle'); setCurrentStep(0); setErrorMsg(''); setEditMode(false);
  };

  const handleProcess = async () => {
    if (!file) return;
    setStatus('processing'); setCurrentStep(1); setStepMessage(STEPS[0].desc);
    const fd = new FormData();
    fd.append('file', file);
    if (isVideo) fd.append('extraction_mode', extractionMode);

    try {
      const res = await fetch(`${API_BASE_URL}/api/process`, { method: 'POST', body: fd });
      if (!res.ok) { const e = await res.json(); throw new Error(e.detail || 'Server error'); }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const chunks = buf.split('\n\n'); buf = chunks.pop();

        for (const chunk of chunks) {
          const ev  = chunk.match(/^event:\s*(.+)$/m)?.[1]?.trim();
          const raw = chunk.match(/^data:\s*(.+)$/m)?.[1]?.trim();
          if (!ev || !raw) continue;

          const payload = JSON.parse(raw);
          if (ev === 'progress') {
            setCurrentStep(payload.step);
            setStepMessage(payload.message);
          } else if (ev === 'done') {
            setResult(payload.result);
            setEdited(payload.result);
            setDocType(payload.doc_type || '');
            setJsonText(JSON.stringify(payload.result, null, 2));
            setStatus('completed');
            setCurrentStep(4);
          } else if (ev === 'error') {
            throw new Error(payload.message);
          }
        }
      }
    } catch (err) {
      setErrorMsg(err.message || 'Failed to connect.');
      setStatus('error');
    }
  };

  const fieldChange  = (k, v) => setEdited(p => ({ ...p, [k]: v }));
  const copyJson     = () => navigator.clipboard.writeText(JSON.stringify(edited, null, 2));
  const downloadJson = () => {
    const a = Object.assign(document.createElement('a'), {
      href: URL.createObjectURL(new Blob([JSON.stringify(edited, null, 2)], { type: 'application/json' })),
      download: `${(file?.name || 'extracted').replace(/\.[^.]+$/, '')}_${docType}.json`,
    });
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
  };

  const groups = FIELD_GROUPS[docType] || null;

  return (
    <div className="app-container">
      <header>
        <div className="logo-section">
          <div className="logo-icon">IP</div>
          <div className="logo-text">
            <h1>Insurance Policy Processor</h1>
            <p>Layout-Aware OCR & Semantic LLM Extraction</p>
          </div>
        </div>
        <div style={{ display:'flex', gap:'0.5rem', alignItems:'center' }}>
          <div className="tab-container">
            <button className={`tab-button ${page==='process'?'active':''}`} onClick={() => setPage('process')}>
              <UploadCloud size={14} /> Process
            </button>
            <button className={`tab-button ${page==='comparison'?'active':''}`} onClick={() => setPage('comparison')}>
              <Zap size={14} /> Compare
            </button>
          </div>
          {page === 'process' && status === 'completed' && (
            <button className="btn btn-secondary" onClick={reset}>
              <RefreshCw size={16} /> New
            </button>
          )}
        </div>
      </header>

      {page === 'comparison' && <ComparisonPage />}

      {page === 'process' && (
        <>
          {status === 'idle' && !file && (
            <div className={`dropzone-container ${dragActive ? 'drag-active' : ''}`}
              onDragEnter={handleDrag} onDragOver={handleDrag} onDragLeave={handleDrag} onDrop={handleDrop}
              onClick={() => fileInputRef.current.click()}>
              <input type="file" className="file-input" ref={fileInputRef}
                onChange={e => e.target.files[0] && validateAndSet(e.target.files[0])}
                accept={VALID_EXTS.join(',')} />
              <UploadCloud className="dropzone-icon animate-pulse-glow" />
              <p className="dropzone-text">Drag & drop your insurance document here</p>
              <p className="dropzone-subtext">PDF · Image · Word · Audio · Video</p>
              <p className="dropzone-subtext" style={{ marginTop:'0.25rem' }}>
                Life · Car · Travel · Health · Property
              </p>
              <button className="btn btn-primary" style={{ marginTop: '1.5rem' }}
                onClick={e => { e.stopPropagation(); fileInputRef.current.click(); }}>Browse Files</button>
            </div>
          )}

          {file && (status === 'idle' || status === 'processing') && (
            <div className="card animate-slide-up" style={{ maxWidth: 600, margin: '0 auto', width: '100%' }}>
              <div className="file-info-bar">
                <div className="file-info-left">
                  <FileText className="file-info-icon" />
                  <div className="file-info-details">
                    <div className="file-info-name">{file.name}</div>
                    <div className="file-info-size">{(file.size/1024/1024).toFixed(2)} MB</div>
                  </div>
                </div>
                {status === 'idle' && <button className="btn btn-secondary" onClick={reset}>Cancel</button>}
              </div>
              {status === 'idle' && isVideo && (
                <div style={{ marginTop: '1.5rem' }}>
                  <span className="field-label" style={{ display:'block', marginBottom:'0.5rem' }}>Video Extraction Mode</span>
                  <div className="tab-container" style={{ display:'grid', gridTemplateColumns:'1fr 1fr 1fr', gap:'0.25rem' }}>
                    {['both','frames','audio'].map(m => (
                      <button key={m} type="button"
                        className={`tab-button ${extractionMode===m?'active':''}`}
                        onClick={() => setExtractionMode(m)}
                        style={{ justifyContent:'center', textTransform:'capitalize' }}>{m}</button>
                    ))}
                  </div>
                </div>
              )}
              {status === 'idle' && (
                <button className="btn btn-primary btn-lg"
                  style={{ width:'100%', marginTop:'2rem', padding:'1rem' }}
                  onClick={handleProcess}>Start Processing</button>
              )}
              {status === 'processing' && (
                <div className="progress-stepper" style={{ marginTop:'1.5rem' }}>
                  <div style={{ display:'flex', alignItems:'center', gap:'0.75rem', justifyContent:'center', marginBottom:'1rem' }}>
                    <Loader2 className="animate-spin" size={22} style={{ color:'#a855f7' }} />
                    <span style={{ fontSize:'0.9rem', fontWeight:500 }}>{stepMessage}</span>
                  </div>
                  {STEPS.map(s => {
                    const done = currentStep > s.id; const active = currentStep === s.id;
                    return (
                      <div key={s.id} className={`progress-step ${active?'active':''} ${done?'completed':''}`}>
                        <div className="step-indicator">{done ? <CheckCircle2 size={16}/> : s.id}</div>
                        <div className="step-text">
                          <div className="step-title">{s.title}</div>
                          <div className="step-desc">{s.desc}</div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          {status === 'error' && (
            <div className="card animate-slide-up" style={{ borderColor:'var(--error)', maxWidth:600, margin:'2rem auto' }}>
              <div className="card-title" style={{ color:'var(--error)' }}><AlertTriangle /> Extraction Failed</div>
              <p style={{ fontSize:'0.95rem', color:'var(--text-secondary)', lineHeight:1.6 }}>{errorMsg}</p>
              <div style={{ marginTop:'1.5rem', display:'flex', gap:'1rem', justifyContent:'flex-end' }}>
                <button className="btn btn-secondary" onClick={reset}>Upload New File</button>
                {file && <button className="btn btn-primary" onClick={handleProcess}>Retry</button>}
              </div>
            </div>
          )}

          {status === 'completed' && edited && (
            <div className="animate-slide-up" style={{ display:'flex', flexDirection:'column', gap:'1.5rem' }}>
              <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', flexWrap:'wrap', gap:'1rem' }}>
                <div className="file-info-left">
                  <FileText className="file-info-icon" />
                  <div className="file-info-details">
                    <span style={{ fontSize:'0.75rem', color:'var(--text-muted)' }}>PROCESSED · {docType.toUpperCase()}</span>
                    <div className="file-info-name">{file?.name}</div>
                  </div>
                </div>
                <div style={{ display:'flex', gap:'0.75rem', flexWrap:'wrap', alignItems:'center' }}>
                  <div className="tab-container">
                    <button className={`tab-button ${activeTab==='cards'?'active':''}`} onClick={() => setActiveTab('cards')}><LayoutGrid size={16}/> Grid</button>
                    <button className={`tab-button ${activeTab==='json'?'active':''}`}  onClick={() => setActiveTab('json')}><FileJson size={16}/> JSON</button>
                  </div>
                  <button className="btn btn-secondary" onClick={copyJson}><Copy size={16}/> Copy</button>
                  <button className="btn btn-secondary" onClick={downloadJson}><Download size={16}/> Download</button>
                  {activeTab === 'cards' && (
                    editMode
                      ? <button className="btn btn-primary"   onClick={() => setEditMode(false)}><CheckCircle2 size={16}/> Done</button>
                      : <button className="btn btn-secondary" onClick={() => setEditMode(true)}><Edit size={16}/> Edit</button>
                  )}
                </div>
              </div>

              {activeTab === 'cards' && (
                <div style={{ display:'flex', flexDirection:'column', gap:'2rem' }}>
                  {groups ? groups.map((g, gi) => (
                    <div key={gi} className="card">
                      <h2 className="card-title">{g.title}</h2>
                      <div className="fields-grid">
                        {g.fields.map(f => {
                          const val = edited[f.key] ?? '';
                          const cls = `field-input${val===''&&!editMode?' field-input-empty':''}`;
                          return (
                            <div key={f.key} className="field-group" style={{ gridColumn: f.textarea ? '1/-1' : 'auto' }}>
                              <span className="field-label">{f.label}</span>
                              {f.textarea
                                ? <textarea className={cls} value={val} rows={3} disabled={!editMode} placeholder={editMode ? f.placeholder : '— Not Found —'} onChange={e => fieldChange(f.key, e.target.value)} />
                                : <input type="text" className={cls} value={val} disabled={!editMode} placeholder={editMode ? f.placeholder : '— Not Found —'} onChange={e => fieldChange(f.key, e.target.value)} />
                              }
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )) : <GenericGroups data={edited} />}
                </div>
              )}

              {activeTab === 'json' && (
                <div className="card" style={{ padding:'1rem' }}>
                  <div className="code-editor-container">
                    <div className="code-editor-header">
                      <div className="code-editor-title">{docType}_output.json</div>
                      <div style={{ display:'flex', gap:'0.5rem' }}>
                        <button className="btn btn-secondary" style={{ padding:'0.25rem 0.75rem', fontSize:'0.75rem' }}
                          onClick={() => setJsonText(JSON.stringify(result, null, 2))}>Reset</button>
                        <button className="btn btn-primary" style={{ padding:'0.25rem 0.75rem', fontSize:'0.75rem' }}
                          onClick={() => { try { setEdited(JSON.parse(jsonText)); setEditMode(false); setErrorMsg(''); } catch { setErrorMsg('Invalid JSON.'); } }}>Save</button>
                      </div>
                    </div>
                    <textarea className="code-editor-textarea" value={jsonText}
                      onChange={e => setJsonText(e.target.value)} spellCheck={false} />
                  </div>
                  {errorMsg && (
                    <div style={{ color:'var(--error)', marginTop:'0.75rem', display:'flex', gap:'0.5rem', fontSize:'0.875rem' }}>
                      <AlertTriangle size={14}/> {errorMsg}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}