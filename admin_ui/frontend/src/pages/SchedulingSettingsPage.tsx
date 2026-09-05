import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import axios from 'axios';
import { CalendarClock, CheckCircle2, RefreshCw, Save, Settings2, Users, Wrench } from 'lucide-react';
import { toast } from 'sonner';
import { describeApiError } from '../utils/apiErrors';

const weekdays = [
    ['mon', 'Monday'], ['tue', 'Tuesday'], ['wed', 'Wednesday'], ['thu', 'Thursday'],
    ['fri', 'Friday'], ['sat', 'Saturday'], ['sun', 'Sunday'],
] as const;

type IntervalMap = Record<string, string[][]>;
type Readiness = {
    ready: boolean; missing: string[]; active_technicians: number; service_assignments: number;
    working_intervals: number; eligible_services: number; schedulable_technicians: number;
};
type SettingsState = {
    timezone: string; business_hours: IntervalMap; scheduling_enabled: boolean;
    minimum_notice_minutes: number | null; same_day_cutoff: string | null;
    travel_buffer_before_minutes: number | null; travel_buffer_after_minutes: number | null;
    preparation_buffer_minutes: number | null; configured?: Record<string, boolean>; readiness: Readiness;
};
type Service = { id: string; display_name: string; active: boolean; duration_minutes: number | null; auto_bookable: boolean };
type Technician = {
    id: string; display_name: string; active: boolean; timezone: string; timezone_override?: string | null;
    inherit_organization_timezone: boolean; service_ids: string[]; working_hours: IntervalMap;
};

const emptyReadiness: Readiness = {
    ready: false, missing: [], active_technicians: 0, service_assignments: 0,
    working_intervals: 0, eligible_services: 0, schedulable_technicians: 0,
};
const emptySettings: SettingsState = {
    timezone: '', business_hours: {}, scheduling_enabled: false, minimum_notice_minutes: null,
    same_day_cutoff: null, travel_buffer_before_minutes: null, travel_buffer_after_minutes: null,
    preparation_buffer_minutes: null, readiness: emptyReadiness,
};
const parseIntervals = (value: string): string[][] => value.trim() ? value.split(',').map(part => {
    const [start, end] = part.trim().split('-').map(item => item.trim());
    return [start, end];
}) : [];
const intervalText = (intervals?: string[][]) => (intervals || []).map(([start, end]) => `${start}-${end}`).join(', ');
const nullableNumber = (value: string) => value === '' ? null : Number(value);

export default function SchedulingSettingsPage() {
    const location = useLocation();
    const initialTab = location.pathname.endsWith('/technicians') ? 'technicians'
        : location.pathname.endsWith('/services') ? 'services' : 'organization';
    const [tab, setTab] = useState(initialTab);
    const [settings, setSettings] = useState<SettingsState>(emptySettings);
    const [services, setServices] = useState<Service[]>([]);
    const [technicians, setTechnicians] = useState<Technician[]>([]);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [technician, setTechnician] = useState<Technician>({
        id: '', display_name: '', active: true, timezone: '', timezone_override: null,
        inherit_organization_timezone: true, service_ids: [], working_hours: {},
    });
    const [editingTechnician, setEditingTechnician] = useState(false);
    const [service, setService] = useState<Service>({
        id: '', display_name: '', active: true, duration_minutes: null, auto_bookable: false,
    });
    const [editingService, setEditingService] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const [settingsResult, servicesResult, techniciansResult] = await Promise.all([
                axios.get('/api/tools/scheduling/settings'),
                axios.get('/api/tools/scheduling/services'),
                axios.get('/api/tools/technicians'),
            ]);
            setSettings({ ...emptySettings, ...settingsResult.data });
            setServices(servicesResult.data.services || []);
            setTechnicians(techniciansResult.data.technicians || []);
        } catch (error) {
            toast.error(describeApiError(error, 'Could not load scheduling configuration'));
        } finally {
            setLoading(false);
        }
    }, []);
    useEffect(() => { void load(); }, [load]);

    const businessHoursReady = useMemo(() =>
        weekdays.some(([key]) => (settings.business_hours[key] || []).length > 0), [settings.business_hours]);
    const setBusinessDay = (day: string, open: boolean, start = '09:00', end = '17:00') => {
        setSettings(current => ({
            ...current,
            business_hours: { ...current.business_hours, [day]: open ? [[start, end]] : [] },
        }));
    };

    const saveSettings = async (event: FormEvent) => {
        event.preventDefault(); setSaving(true);
        try {
            const response = await axios.put('/api/tools/scheduling/settings', settings);
            setSettings(current => ({ ...current, ...response.data }));
            toast.success(response.data.apply_required
                ? `Saved. Apply via ${response.data.recommended_apply_method}.`
                : 'Organization scheduling settings saved.');
        } catch (error) { toast.error(describeApiError(error, 'Could not save organization settings')); }
        finally { setSaving(false); }
    };

    const resetTechnician = () => {
        setTechnician({ id: '', display_name: '', active: true, timezone: '', timezone_override: null,
            inherit_organization_timezone: true, service_ids: [], working_hours: {} });
        setEditingTechnician(false);
    };
    const saveTechnician = async (event: FormEvent) => {
        event.preventDefault(); setSaving(true);
        try {
            const payload = { ...technician, timezone: technician.inherit_organization_timezone ? '' : technician.timezone };
            if (editingTechnician) await axios.put(`/api/tools/technicians/${encodeURIComponent(technician.id)}`, payload);
            else await axios.post('/api/tools/technicians', payload);
            toast.success(editingTechnician ? 'Technician updated.' : 'Technician created.');
            resetTechnician(); await load();
        } catch (error) { toast.error(describeApiError(error, 'Could not save technician')); }
        finally { setSaving(false); }
    };
    const chooseTechnician = (item: Technician) => {
        setTechnician({ ...item, service_ids: item.service_ids || [], working_hours: item.working_hours || {} });
        setEditingTechnician(true); setTab('technicians');
    };

    const resetService = () => { setService({ id: '', display_name: '', active: true, duration_minutes: null, auto_bookable: false }); setEditingService(false); };
    const saveService = async (event: FormEvent) => {
        event.preventDefault(); setSaving(true);
        try {
            if (editingService) await axios.put(`/api/tools/scheduling/services/${encodeURIComponent(service.id)}`, service);
            else await axios.post('/api/tools/scheduling/services', service);
            toast.success(editingService ? 'Service updated.' : 'Service created.');
            resetService(); await load();
        } catch (error) { toast.error(describeApiError(error, 'Could not save service')); }
        finally { setSaving(false); }
    };

    if (loading) return <div className="p-8 text-muted-foreground">Loading scheduling configuration…</div>;
    const readiness = settings.readiness || emptyReadiness;
    const tabs = [
        ['organization', 'Organization Settings', Settings2], ['technicians', 'Technicians', Users], ['services', 'Service Catalog', Wrench],
    ] as const;

    return <div className="p-4 md:p-8 space-y-6 max-w-7xl mx-auto">
        <div className="flex flex-wrap items-center justify-between gap-3">
            <div><h1 className="text-2xl font-semibold">Scheduling Administration</h1>
                <p className="text-sm text-muted-foreground">Configuration is read in real time by the same availability engine Sarah uses.</p></div>
            <div className="flex gap-2"><Link className="border rounded-md px-3 py-2 text-sm" to="/admin/schedule"><CalendarClock className="inline w-4 h-4 mr-2" />Calendar</Link>
                <button type="button" className="border rounded-md px-3 py-2 text-sm" onClick={() => void load()}><RefreshCw className="inline w-4 h-4 mr-2" />Refresh</button></div>
        </div>

        <section className="border rounded-lg p-4 bg-card">
            <div className="flex items-center gap-2"><CheckCircle2 className={`w-5 h-5 ${readiness.ready ? 'text-green-600' : 'text-amber-600'}`} />
                <h2 className="font-semibold">Scheduling readiness: {readiness.ready ? 'Ready' : 'Configuration required'}</h2></div>
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mt-3 text-sm">
                <span>Timezone: {settings.timezone ? 'Configured' : 'Missing'}</span><span>Business hours: {businessHoursReady ? 'Configured' : 'Missing'}</span>
                <span>Active technicians: {readiness.active_technicians}</span><span>Eligible services: {readiness.eligible_services}</span>
                <span>Recurring intervals: {readiness.working_intervals}</span>
            </div>
            {!readiness.ready && <p className="text-sm text-amber-700 mt-2">Missing: {(readiness.missing || []).join(', ') || 'configuration validation'}. Scheduling remains fail-closed.</p>}
        </section>

        <div className="flex flex-wrap gap-2 border-b pb-3">{tabs.map(([key, label, Icon]) =>
            <button type="button" key={key} onClick={() => setTab(key)} className={`px-3 py-2 rounded-md text-sm ${tab === key ? 'bg-primary text-primary-foreground' : 'bg-muted'}`}>
                <Icon className="inline w-4 h-4 mr-2" />{label}</button>)}</div>

        {tab === 'organization' && <form onSubmit={saveSettings} className="space-y-6">
            <section className="border rounded-lg p-4 bg-card space-y-4">
                <h2 className="font-semibold">Organization boundaries</h2>
                <label className="block text-sm max-w-md">Timezone
                    <input required className="mt-1 w-full border rounded-md px-3 py-2 bg-background" value={settings.timezone} onChange={e => setSettings({ ...settings, timezone: e.target.value })} placeholder="IANA timezone" /></label>
                <div className="space-y-2">{weekdays.map(([key, label]) => {
                    const interval = settings.business_hours[key]?.[0]; const open = Boolean(interval);
                    return <div key={key} className="grid grid-cols-[7rem_5rem_1fr_1fr] gap-2 items-center text-sm">
                        <span>{label}</span><label><input type="checkbox" checked={open} onChange={e => setBusinessDay(key, e.target.checked)} /> Open</label>
                        <input aria-label={`${label} start`} type="time" disabled={!open} className="border rounded px-2 py-1 bg-background" value={interval?.[0] || ''} onChange={e => setBusinessDay(key, true, e.target.value, interval?.[1] || '17:00')} />
                        <input aria-label={`${label} end`} type="time" disabled={!open} className="border rounded px-2 py-1 bg-background" value={interval?.[1] || ''} onChange={e => setBusinessDay(key, true, interval?.[0] || '09:00', e.target.value)} />
                    </div>;
                })}</div>
            </section>
            <section className="border rounded-lg p-4 bg-card space-y-4"><h2 className="font-semibold">Booking rules</h2>
                <p className="text-xs text-muted-foreground">Blank means unresolved/engine default; configured values are business policy.</p>
                <div className="grid md:grid-cols-3 gap-4">
                    {([
                        ['minimum_notice_minutes', 'Minimum notice (minutes)'], ['travel_buffer_before_minutes', 'Travel before (minutes)'],
                        ['travel_buffer_after_minutes', 'Travel after (minutes)'], ['preparation_buffer_minutes', 'Preparation (minutes)'],
                    ] as const).map(([key, label]) => <label key={key} className="text-sm">{label}
                        <input type="number" min="0" className="mt-1 w-full border rounded-md px-3 py-2 bg-background" value={settings[key] ?? ''} onChange={e => setSettings({ ...settings, [key]: nullableNumber(e.target.value) })} />
                        <span className="block text-xs text-muted-foreground mt-1">{settings.configured?.[key] ? 'Business-configured' : 'Unresolved/default'}</span></label>)}
                    <label className="text-sm">Same-day cutoff<input type="time" className="mt-1 w-full border rounded-md px-3 py-2 bg-background" value={settings.same_day_cutoff || ''} onChange={e => setSettings({ ...settings, same_day_cutoff: e.target.value || null })} />
                        <span className="block text-xs text-muted-foreground mt-1">{settings.configured?.same_day_cutoff ? 'Business-configured' : 'Unresolved/default'}</span></label>
                </div>
                <label className="flex items-center gap-3 border rounded-md p-3"><input type="checkbox" checked={settings.scheduling_enabled} onChange={e => setSettings({ ...settings, scheduling_enabled: e.target.checked })} />
                    <span><strong>Enable scheduling</strong><small className="block text-muted-foreground">The server rejects enablement until readiness passes.</small></span></label>
                <button disabled={saving} className="bg-primary text-primary-foreground px-4 py-2 rounded-md"><Save className="inline w-4 h-4 mr-2" />Save organization settings</button>
            </section>
        </form>}

        {tab === 'technicians' && <div className="grid lg:grid-cols-[1fr_2fr] gap-6">
            <section className="border rounded-lg p-4 bg-card"><h2 className="font-semibold mb-3">Production technicians</h2>
                {technicians.length === 0 && <p className="text-sm text-muted-foreground">No technicians configured. Availability remains disabled.</p>}
                <div className="space-y-2">{technicians.map(item => <button type="button" key={item.id} onClick={() => chooseTechnician(item)} className="text-left w-full border rounded-md p-3 hover:bg-muted">
                    <strong>{item.display_name || item.id}</strong><span className="block text-xs text-muted-foreground">{item.id} · {item.active ? 'Active' : 'Inactive'} · {item.timezone}</span></button>)}</div>
            </section>
            <form onSubmit={saveTechnician} className="border rounded-lg p-4 bg-card space-y-4"><h2 className="font-semibold">{editingTechnician ? 'Edit technician' : 'Create technician'}</h2>
                <div className="grid md:grid-cols-2 gap-3"><label className="text-sm">Stable ID<input required disabled={editingTechnician} className="mt-1 w-full border rounded px-3 py-2 bg-background" value={technician.id} onChange={e => setTechnician({ ...technician, id: e.target.value })} /></label>
                    <label className="text-sm">Display name<input className="mt-1 w-full border rounded px-3 py-2 bg-background" value={technician.display_name} onChange={e => setTechnician({ ...technician, display_name: e.target.value })} /></label></div>
                <div className="flex flex-wrap gap-5 text-sm"><label><input type="checkbox" checked={technician.active} onChange={e => setTechnician({ ...technician, active: e.target.checked })} /> Active</label>
                    <label><input type="checkbox" checked={technician.inherit_organization_timezone} onChange={e => setTechnician({ ...technician, inherit_organization_timezone: e.target.checked })} /> Inherit organization timezone ({settings.timezone || 'not configured'})</label></div>
                {!technician.inherit_organization_timezone && <label className="block text-sm">Timezone override<input required className="mt-1 w-full border rounded px-3 py-2 bg-background" value={technician.timezone_override || technician.timezone || ''} onChange={e => setTechnician({ ...technician, timezone: e.target.value, timezone_override: e.target.value })} /></label>}
                <fieldset><legend className="text-sm font-medium">Supported services</legend><div className="grid md:grid-cols-2 gap-2 mt-2">{services.filter(item => item.active).map(item => <label key={item.id} className="text-sm"><input type="checkbox" checked={technician.service_ids.includes(item.id)} onChange={e => setTechnician(current => ({ ...current, service_ids: e.target.checked ? [...current.service_ids, item.id] : current.service_ids.filter(id => id !== item.id) }))} /> {item.display_name} <span className="text-muted-foreground">({item.id})</span></label>)}</div></fieldset>
                <fieldset><legend className="text-sm font-medium">Recurring working hours</legend><p className="text-xs text-muted-foreground">Comma-separated split intervals, for example 08:00-12:00, 13:00-17:00. Leave blank for a day off.</p>
                    <div className="grid md:grid-cols-2 gap-2 mt-2">{weekdays.map(([key, label]) => <label key={key} className="text-sm">{label}<input className="mt-1 w-full border rounded px-2 py-1 bg-background" value={intervalText(technician.working_hours[key])} onChange={e => setTechnician({ ...technician, working_hours: { ...technician.working_hours, [key]: parseIntervals(e.target.value) } })} /></label>)}</div></fieldset>
                <div className="flex gap-2"><button disabled={saving} className="bg-primary text-primary-foreground px-4 py-2 rounded-md">Save technician</button>{editingTechnician && <button type="button" className="border px-4 py-2 rounded-md" onClick={resetTechnician}>New technician</button>}
                    {editingTechnician && <Link className="border px-4 py-2 rounded-md" to={`/admin/schedule?technician=${encodeURIComponent(technician.id)}`}>Appointments / exceptions</Link>}</div>
            </form>
        </div>}

        {tab === 'services' && <div className="grid lg:grid-cols-[1fr_1fr] gap-6"><section className="border rounded-lg p-4 bg-card"><h2 className="font-semibold mb-3">Service catalog</h2>
            <div className="space-y-2">{services.map(item => <button type="button" key={item.id} onClick={() => { setService(item); setEditingService(true); }} className="w-full text-left border rounded-md p-3 hover:bg-muted"><strong>{item.display_name}</strong><span className="block text-xs text-muted-foreground">{item.id} · {item.active ? 'Active' : 'Inactive'} · {item.duration_minutes ? `${item.duration_minutes} minutes` : 'Duration unresolved'} · {item.auto_bookable ? 'Auto-bookable' : 'Manual review'}</span></button>)}</div></section>
            <form onSubmit={saveService} className="border rounded-lg p-4 bg-card space-y-4"><h2 className="font-semibold">{editingService ? 'Edit service' : 'Add service'}</h2>
                <label className="block text-sm">Service ID<input required disabled={editingService} className="mt-1 w-full border rounded px-3 py-2 bg-background" value={service.id} onChange={e => setService({ ...service, id: e.target.value })} /></label>
                <label className="block text-sm">Display name<input required className="mt-1 w-full border rounded px-3 py-2 bg-background" value={service.display_name} onChange={e => setService({ ...service, display_name: e.target.value })} /></label>
                <label className="block text-sm">Duration (minutes)<input type="number" min="1" className="mt-1 w-full border rounded px-3 py-2 bg-background" value={service.duration_minutes ?? ''} onChange={e => setService({ ...service, duration_minutes: nullableNumber(e.target.value) })} /></label>
                <div className="flex gap-5 text-sm"><label><input type="checkbox" checked={service.active} onChange={e => setService({ ...service, active: e.target.checked })} /> Active</label><label><input type="checkbox" checked={service.auto_bookable} onChange={e => setService({ ...service, auto_bookable: e.target.checked })} /> Auto-bookable</label></div>
                <div className="flex gap-2"><button disabled={saving} className="bg-primary text-primary-foreground px-4 py-2 rounded-md">Save service</button>{editingService && <button type="button" className="border px-4 py-2 rounded-md" onClick={resetService}>New service</button>}</div>
            </form></div>}
    </div>;
}
