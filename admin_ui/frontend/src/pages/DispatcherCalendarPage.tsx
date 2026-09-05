import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import axios from 'axios';
import {
    CalendarDays,
    ChevronLeft,
    ChevronRight,
    Clock,
    Plus,
    RefreshCw,
    ShieldAlert,
    UserRound,
    X,
} from 'lucide-react';
import { toast } from 'sonner';
import { describeApiError } from '../utils/apiErrors';

type CalendarView = 'day' | 'week';
type EventType = 'appointment' | 'time_off' | 'schedule_block' | 'working_hours';
type Technician = {
    id: string;
    display_name: string;
    active: boolean;
    timezone: string;
    service_ids?: string[];
    working_hours?: Record<string, string[][]>;
};
type CalendarEvent = {
    id: string;
    type: EventType;
    technician_id: string;
    start: string;
    end: string;
    status: string;
    title: string;
    reason?: string;
    service_code?: string;
    customer_name?: string;
    confirmation_ref?: string;
};
type Service = { id: string; display_name: string; active: boolean; duration_minutes?: number };
type CalendarResponse = {
    technicians: Technician[];
    events: CalendarEvent[];
    services: Service[];
    timezone: string;
    business_hours: Record<string, string[] | string[][]>;
    scheduling_enabled: boolean;
};
type Slot = { start: string; end: string; technician_id: string; slot_token?: string };
type Preview = {
    status: string;
    reason?: string;
    slots: Slot[];
    diagnostics?: Array<Record<string, string>>;
};

const startOfDay = (value: Date) => {
    const result = new Date(value);
    result.setHours(0, 0, 0, 0);
    return result;
};
const addDays = (value: Date, amount: number) => {
    const result = new Date(value);
    result.setDate(result.getDate() + amount);
    return result;
};
const rangeFor = (anchor: Date, view: CalendarView) => {
    if (view === 'day') {
        const start = startOfDay(anchor);
        return { start, end: addDays(start, 1), days: [start] };
    }
    const start = startOfDay(anchor);
    start.setDate(start.getDate() - ((start.getDay() + 6) % 7));
    return {
        start,
        end: addDays(start, 7),
        days: Array.from({ length: 7 }, (_, i) => addDays(start, i)),
    };
};
const dateInput = (value: Date) =>
    new Date(value.getTime() - value.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
const dateTimeLabel = (value: string, timezone: string) =>
    new Intl.DateTimeFormat(undefined, {
        timeZone: timezone,
        hour: 'numeric',
        minute: '2-digit',
    }).format(new Date(value));
const dayKey = (value: string, timezone: string) => {
    const parts = new Intl.DateTimeFormat('en', {
        timeZone: timezone,
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
    }).formatToParts(new Date(value));
    const get = (type: Intl.DateTimeFormatPartTypes) =>
        parts.find(part => part.type === type)?.value || '';
    return `${get('year')}-${get('month')}-${get('day')}`;
};
const zonedLocalToIso = (value: string, timezone: string) => {
    const [datePart, timePart] = value.split('T');
    const [year, month, day] = datePart.split('-').map(Number);
    const [hour, minute] = timePart.split(':').map(Number);
    const desiredUtc = Date.UTC(year, month - 1, day, hour, minute);
    let candidate = desiredUtc;
    for (let attempt = 0; attempt < 2; attempt += 1) {
        const parts = new Intl.DateTimeFormat('en', {
            timeZone: timezone,
            hour12: false,
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
        }).formatToParts(new Date(candidate));
        const get = (type: Intl.DateTimeFormatPartTypes) =>
            Number(parts.find(part => part.type === type)?.value || 0);
        const representedUtc = Date.UTC(
            get('year'),
            get('month') - 1,
            get('day'),
            get('hour') % 24,
            get('minute')
        );
        candidate += desiredUtc - representedUtc;
    }
    return new Date(candidate).toISOString();
};
const eventStyle: Record<EventType, string> = {
    appointment: 'border-blue-500/40 bg-blue-500/10 text-blue-900 dark:text-blue-100',
    time_off: 'border-rose-500/40 bg-rose-500/10 text-rose-900 dark:text-rose-100',
    schedule_block: 'border-amber-500/40 bg-amber-500/10 text-amber-900 dark:text-amber-100',
    working_hours: 'border-emerald-500/30 bg-emerald-500/5 text-emerald-900 dark:text-emerald-100',
};
const eventLabel: Record<EventType, string> = {
    appointment: 'Customer appointment',
    time_off: 'Time off',
    schedule_block: 'Manual block',
    working_hours: 'Working hours',
};
const parseSchedule = (values: Record<string, string>) => {
    const result: Record<string, string[][]> = {};
    Object.entries(values).forEach(([day, raw]) => {
        const intervals = raw
            .split(',')
            .map(item => item.trim())
            .filter(Boolean)
            .map(item => {
                const [start, end] = item.split('-').map(part => part.trim());
                if (!/^\d{2}:\d{2}$/.test(start || '') || !/^\d{2}:\d{2}$/.test(end || ''))
                    throw new Error(`${day.toUpperCase()} must use HH:MM-HH:MM intervals`);
                return [start, end];
            });
        if (intervals.length) result[day] = intervals;
    });
    return result;
};

export default function DispatcherCalendarPage() {
    const [anchor, setAnchor] = useState(startOfDay(new Date()));
    const [view, setView] = useState<CalendarView>('week');
    const [data, setData] = useState<CalendarResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [activeOnly, setActiveOnly] = useState(true);
    const [technicianId, setTechnicianId] = useState('');
    const [serviceFilter, setServiceFilter] = useState('');
    const [exceptionOpen, setExceptionOpen] = useState(false);
    const [exceptionType, setExceptionType] = useState<'time-off' | 'blocks'>('time-off');
    const [exceptionStart, setExceptionStart] = useState('');
    const [exceptionEnd, setExceptionEnd] = useState('');
    const [exceptionReason, setExceptionReason] = useState('');
    const [previewService, setPreviewService] = useState('');
    const [previewDate, setPreviewDate] = useState(dateInput(new Date()));
    const [preview, setPreview] = useState<Preview | null>(null);
    const [scheduleTech, setScheduleTech] = useState<Technician | null>(null);
    const [scheduleValues, setScheduleValues] = useState<Record<string, string>>({});
    const [rescheduleAppointment, setRescheduleAppointment] = useState<CalendarEvent | null>(null);
    const [rescheduleSlots, setRescheduleSlots] = useState<Slot[]>([]);
    const timezoneRef = useRef('America/Vancouver');
    const range = useMemo(() => rangeFor(anchor, view), [anchor, view]);

    const loadCalendar = useCallback(async () => {
        setLoading(true);
        try {
            const timezone = timezoneRef.current;
            const response = await axios.get<CalendarResponse>('/api/tools/calendar', {
                params: {
                    start: zonedLocalToIso(`${dateInput(range.start)}T00:00`, timezone),
                    end: zonedLocalToIso(`${dateInput(range.end)}T00:00`, timezone),
                    technician_ids: technicianId,
                    active_only: activeOnly,
                    service_code: serviceFilter,
                },
            });
            timezoneRef.current = response.data.timezone || timezoneRef.current;
            setData(response.data);
            setPreviewService(
                current => current || response.data.services.find(item => item.active)?.id || ''
            );
        } catch (error) {
            toast.error(describeApiError(error, 'Could not load dispatcher calendar'));
        } finally {
            setLoading(false);
        }
    }, [activeOnly, range.end, range.start, serviceFilter, technicianId]);
    useEffect(() => {
        void loadCalendar();
    }, [loadCalendar]);

    const groupedEvents = useMemo(() => {
        const grouped: Record<string, CalendarEvent[]> = {};
        for (const event of data?.events || [])
            (grouped[dayKey(event.start, data?.timezone || 'America/Vancouver')] ||= []).push(
                event
            );
        return grouped;
    }, [data]);

    const cancelException = async (event: CalendarEvent) => {
        const segment = event.type === 'time_off' ? 'time-off' : 'blocks';
        if (!window.confirm(`Cancel this ${eventLabel[event.type].toLowerCase()}?`)) return;
        try {
            await axios.delete(
                `/api/tools/technicians/${encodeURIComponent(event.technician_id)}/${segment}/${encodeURIComponent(event.id)}`
            );
            toast.success('Calendar exception cancelled');
            await loadCalendar();
        } catch (error) {
            toast.error(describeApiError(error, 'Could not cancel calendar exception'));
        }
    };
    const createException = async (event: FormEvent) => {
        event.preventDefault();
        if (!technicianId) return toast.error('Select one technician first');
        try {
            const timezone =
                data?.technicians.find(item => item.id === technicianId)?.timezone ||
                data?.timezone ||
                'America/Vancouver';
            await axios.post(
                `/api/tools/technicians/${encodeURIComponent(technicianId)}/${exceptionType}`,
                {
                    start_datetime: zonedLocalToIso(exceptionStart, timezone),
                    end_datetime: zonedLocalToIso(exceptionEnd, timezone),
                    reason: exceptionReason,
                }
            );
            setExceptionOpen(false);
            setExceptionReason('');
            toast.success(exceptionType === 'time-off' ? 'Time off added' : 'Schedule block added');
            await loadCalendar();
        } catch (error) {
            toast.error(describeApiError(error, 'Could not add calendar exception'));
        }
    };
    const cancelAppointment = async (event: CalendarEvent) => {
        if (
            !window.confirm('Cancel this customer appointment? This immediately releases capacity.')
        )
            return;
        try {
            await axios.post(
                `/api/tools/calendar/appointments/${encodeURIComponent(event.id)}/cancel`
            );
            toast.success('Appointment cancelled; capacity is available immediately');
            await loadCalendar();
        } catch (error) {
            toast.error(describeApiError(error, 'Could not cancel appointment'));
        }
    };
    const loadRescheduleOptions = async (event: CalendarEvent) => {
        try {
            const response = await axios.get<Preview>(
                `/api/tools/calendar/appointments/${encodeURIComponent(event.id)}/reschedule-options`,
                { params: { start_date: previewDate, days: view === 'day' ? 1 : 7 } }
            );
            setRescheduleAppointment(event);
            setRescheduleSlots(response.data.slots || []);
            if (!response.data.slots?.length)
                toast.info(response.data.reason || 'No replacement slots found');
        } catch (error) {
            toast.error(describeApiError(error, 'Could not load replacement slots'));
        }
    };
    const applyReschedule = async (slot: Slot) => {
        if (!rescheduleAppointment || !slot.slot_token) return;
        try {
            const response = await axios.post(
                `/api/tools/calendar/appointments/${encodeURIComponent(rescheduleAppointment.id)}/reschedule`,
                { slot_token: slot.slot_token }
            );
            toast.success(
                response.data?.sms?.sent
                    ? 'Appointment rescheduled and SMS queued'
                    : 'Appointment rescheduled; SMS needs staff follow-up'
            );
            setRescheduleAppointment(null);
            setRescheduleSlots([]);
            await loadCalendar();
        } catch (error) {
            toast.error(
                describeApiError(
                    error,
                    'Slot changed before it could be reserved; load fresh options'
                )
            );
        }
    };
    const runPreview = async () => {
        if (!previewService) return;
        try {
            const response = await axios.get<Preview>('/api/tools/calendar/availability-preview', {
                params: {
                    service_code: previewService,
                    start_date: previewDate,
                    days: view === 'day' ? 1 : 7,
                    technician_id: technicianId,
                },
            });
            setPreview(response.data);
        } catch (error) {
            toast.error(describeApiError(error, 'Could not calculate availability'));
        }
    };
    const openScheduleEditor = (technician: Technician) => {
        const values: Record<string, string> = {};
        Object.entries(technician.working_hours || {}).forEach(([day, intervals]) => {
            values[day] = intervals.map(([start, end]) => `${start}-${end}`).join(', ');
        });
        setScheduleValues(values);
        setScheduleTech(technician);
    };
    const saveSchedule = async (event: FormEvent) => {
        event.preventDefault();
        if (!scheduleTech) return;
        try {
            await axios.put(`/api/tools/technicians/${encodeURIComponent(scheduleTech.id)}`, {
                id: scheduleTech.id,
                display_name: scheduleTech.display_name,
                active: scheduleTech.active,
                timezone: scheduleTech.timezone,
                service_ids: scheduleTech.service_ids || [],
                working_hours: parseSchedule(scheduleValues),
            });
            setScheduleTech(null);
            toast.success('Recurring technician hours updated');
            await loadCalendar();
        } catch (error) {
            toast.error(
                error instanceof Error
                    ? error.message
                    : describeApiError(error, 'Could not update schedule')
            );
        }
    };
    const shift = view === 'day' ? 1 : 7;
    const timezone = data?.timezone || 'America/Vancouver';
    const businessHoursLabel = Object.entries(data?.business_hours || {})
        .map(([day, intervals]) => {
            const normalized = Array.isArray(intervals[0])
                ? (intervals as string[][])
                : [intervals as string[]];
            return `${day.toUpperCase()} ${normalized.map(item => item.join('–')).join(', ')}`;
        })
        .join(' · ');
    const exclusionCounts = (preview?.diagnostics || []).reduce<Record<string, number>>(
        (counts, item) => {
            const reason = item.reason || 'excluded';
            counts[reason] = (counts[reason] || 0) + 1;
            return counts;
        },
        {}
    );

    return (
        <div className="space-y-6 pb-10">
            <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
                <div>
                    <h1 className="text-2xl font-bold flex items-center gap-2">
                        <CalendarDays className="h-6 w-6" /> Dispatcher Calendar
                    </h1>
                    <p className="text-sm text-muted-foreground mt-1">
                        Appointments and exceptions update Sarah&apos;s deterministic availability
                        immediately.
                    </p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                    <button
                        className="btn-secondary"
                        onClick={() => setAnchor(startOfDay(new Date()))}
                    >
                        Today
                    </button>
                    <button
                        className="btn-secondary p-2"
                        aria-label="Previous date"
                        onClick={() => setAnchor(addDays(anchor, -shift))}
                    >
                        <ChevronLeft className="h-4 w-4" />
                    </button>
                    <button
                        className="btn-secondary p-2"
                        aria-label="Next date"
                        onClick={() => setAnchor(addDays(anchor, shift))}
                    >
                        <ChevronRight className="h-4 w-4" />
                    </button>
                    <div className="flex rounded-md border border-border p-1">
                        {(['day', 'week'] as CalendarView[]).map(item => (
                            <button
                                key={item}
                                onClick={() => setView(item)}
                                className={`px-3 py-1 text-sm rounded ${view === item ? 'bg-primary text-primary-foreground' : ''}`}
                            >
                                {item[0].toUpperCase() + item.slice(1)}
                            </button>
                        ))}
                    </div>
                    <button
                        className="btn-secondary p-2"
                        aria-label="Refresh"
                        onClick={() => void loadCalendar()}
                    >
                        <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
                    </button>
                </div>
            </div>
            {!data?.scheduling_enabled && (
                <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-4 flex gap-3">
                    <ShieldAlert className="h-5 w-5 text-amber-600 shrink-0" />
                    <div>
                        <p className="font-medium">Production availability is disabled.</p>
                        <p className="text-sm text-muted-foreground">
                            Configure approved business days and real technician capacity before
                            enabling scheduling. No slots will be fabricated.
                        </p>
                    </div>
                </div>
            )}
            <div className="rounded-lg border border-border bg-card px-4 py-3 text-sm">
                <span className="font-medium">Organization business hours:</span>{' '}
                <span className="text-muted-foreground">
                    {businessHoursLabel || 'Not configured'} ({timezone})
                </span>
                <p className="text-xs text-muted-foreground mt-1">
                    Green calendar boundaries are technician recurring hours, which remain separate
                    from organization hours.
                </p>
            </div>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4 rounded-lg border border-border bg-card p-4">
                <label className="text-sm">
                    Technician
                    <select
                        className="input mt-1 w-full"
                        value={technicianId}
                        onChange={e => setTechnicianId(e.target.value)}
                    >
                        <option value="">All active technicians</option>
                        {(data?.technicians || []).map(tech => (
                            <option key={tech.id} value={tech.id}>
                                {tech.display_name || tech.id}
                            </option>
                        ))}
                    </select>
                </label>
                <label className="text-sm">
                    Appointment service
                    <select
                        className="input mt-1 w-full"
                        value={serviceFilter}
                        onChange={e => setServiceFilter(e.target.value)}
                    >
                        <option value="">All services</option>
                        {(data?.services || [])
                            .filter(item => item.active)
                            .map(item => (
                                <option key={item.id} value={item.id}>
                                    {item.display_name}
                                </option>
                            ))}
                    </select>
                </label>
                <label className="flex items-end gap-2 pb-2 text-sm">
                    <input
                        type="checkbox"
                        checked={activeOnly}
                        onChange={e => setActiveOnly(e.target.checked)}
                    />{' '}
                    Active technicians only
                </label>
                <div className="flex items-end">
                    <button
                        className="btn-primary w-full flex justify-center items-center gap-2"
                        disabled={!technicianId}
                        onClick={() => setExceptionOpen(true)}
                    >
                        <Plus className="h-4 w-4" /> Add exception
                    </button>
                </div>
            </div>
            {!loading && data?.technicians.length === 0 ? (
                <div className="rounded-lg border border-dashed border-border p-10 text-center">
                    <UserRound className="h-10 w-10 mx-auto text-muted-foreground" />
                    <h2 className="font-semibold mt-3">
                        No production technicians are configured.
                    </h2>
                    <p className="text-sm text-muted-foreground mt-1">
                        Availability remains disabled until real technician schedules and service
                        eligibility are entered.
                    </p>
                </div>
            ) : (
                <div
                    className={`grid gap-3 ${view === 'week' ? 'md:grid-cols-2 xl:grid-cols-7' : 'grid-cols-1'}`}
                >
                    {range.days.map(day => {
                        const key = dateInput(day);
                        const events = groupedEvents[key] || [];
                        return (
                            <section
                                key={key}
                                className="min-h-52 rounded-lg border border-border bg-card overflow-hidden"
                            >
                                <header className="border-b border-border px-3 py-2 bg-muted/40">
                                    <p className="text-xs uppercase text-muted-foreground">
                                        {day.toLocaleDateString(undefined, { weekday: 'short' })}
                                    </p>
                                    <p className="font-semibold">
                                        {day.toLocaleDateString(undefined, {
                                            month: 'short',
                                            day: 'numeric',
                                        })}
                                    </p>
                                </header>
                                <div className="p-2 space-y-2">
                                    {events.length === 0 && (
                                        <p className="p-3 text-xs text-muted-foreground text-center">
                                            No schedule records
                                        </p>
                                    )}
                                    {events.map(item => (
                                        <article
                                            key={`${item.type}:${item.id}`}
                                            className={`rounded border p-2 text-xs ${eventStyle[item.type]}`}
                                        >
                                            <div className="font-semibold">
                                                {dateTimeLabel(item.start, timezone)}–
                                                {dateTimeLabel(item.end, timezone)}
                                            </div>
                                            <div>
                                                {eventLabel[item.type]} · {item.technician_id}
                                                {item.type === 'appointment'
                                                    ? ` · ${item.status}`
                                                    : ''}
                                            </div>
                                            {item.customer_name && (
                                                <div className="truncate">{item.customer_name}</div>
                                            )}
                                            {item.title && item.type !== 'working_hours' && (
                                                <div className="truncate opacity-80">
                                                    {item.title}
                                                </div>
                                            )}
                                            {(item.type === 'time_off' ||
                                                item.type === 'schedule_block') && (
                                                <button
                                                    className="mt-2 underline"
                                                    onClick={() => void cancelException(item)}
                                                >
                                                    Cancel
                                                </button>
                                            )}
                                            {item.type === 'appointment' && (
                                                <div className="flex gap-2 mt-2">
                                                    <button
                                                        className="underline"
                                                        onClick={() =>
                                                            void loadRescheduleOptions(item)
                                                        }
                                                    >
                                                        Reschedule
                                                    </button>
                                                    <button
                                                        className="underline text-destructive"
                                                        onClick={() => void cancelAppointment(item)}
                                                    >
                                                        Cancel
                                                    </button>
                                                </div>
                                            )}
                                        </article>
                                    ))}
                                </div>
                            </section>
                        );
                    })}
                </div>
            )}
            <section className="rounded-lg border border-border bg-card p-4 space-y-4">
                <div>
                    <h2 className="font-semibold">Availability Preview</h2>
                    <p className="text-sm text-muted-foreground">
                        Uses the same backend availability engine Sarah calls. Previewed slots are
                        still revalidated when booked.
                    </p>
                </div>
                <div className="grid gap-3 md:grid-cols-4">
                    <select
                        className="input"
                        value={previewService}
                        onChange={e => setPreviewService(e.target.value)}
                    >
                        <option value="">Select service</option>
                        {(data?.services || [])
                            .filter(item => item.active)
                            .map(item => (
                                <option key={item.id} value={item.id}>
                                    {item.display_name} ({item.duration_minutes || '?'} min)
                                </option>
                            ))}
                    </select>
                    <input
                        className="input"
                        type="date"
                        value={previewDate}
                        onChange={e => setPreviewDate(e.target.value)}
                    />
                    <select
                        className="input"
                        value={technicianId}
                        onChange={e => setTechnicianId(e.target.value)}
                    >
                        <option value="">Any eligible technician</option>
                        {(data?.technicians || []).map(tech => (
                            <option key={tech.id} value={tech.id}>
                                {tech.display_name}
                            </option>
                        ))}
                    </select>
                    <button
                        className="btn-primary"
                        disabled={!previewService}
                        onClick={() => void runPreview()}
                    >
                        Calculate real slots
                    </button>
                </div>
                {preview && (
                    <div className="rounded-md bg-muted/40 p-3 text-sm">
                        <p className="font-medium">Result: {preview.status}</p>
                        {preview.reason && (
                            <p className="text-muted-foreground">{preview.reason}</p>
                        )}
                        {Object.keys(exclusionCounts).length > 0 && (
                            <p className="text-xs text-muted-foreground mt-2">
                                Excluded candidates:{' '}
                                {Object.entries(exclusionCounts)
                                    .map(([reason, count]) => `${reason} (${count})`)
                                    .join(', ')}
                            </p>
                        )}
                        <div className="mt-2 grid gap-2 md:grid-cols-3">
                            {preview.slots.map(slot => (
                                <div
                                    key={`${slot.technician_id}:${slot.start}`}
                                    className="rounded border border-border bg-background p-2"
                                >
                                    <Clock className="inline h-3 w-3 mr-1" />
                                    {new Date(slot.start).toLocaleString()} · {slot.technician_id}
                                </div>
                            ))}
                        </div>
                    </div>
                )}
            </section>
            {(data?.technicians.length || 0) > 0 && (
                <section className="rounded-lg border border-border bg-card p-4">
                    <h2 className="font-semibold mb-3">Recurring Technician Hours</h2>
                    <div className="flex flex-wrap gap-2">
                        {data?.technicians.map(tech => (
                            <button
                                key={tech.id}
                                className="btn-secondary"
                                onClick={() => openScheduleEditor(tech)}
                            >
                                {tech.display_name || tech.id}: edit hours
                            </button>
                        ))}
                    </div>
                </section>
            )}
            {exceptionOpen && (
                <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
                    <form
                        onSubmit={createException}
                        className="w-full max-w-lg rounded-lg bg-card border border-border p-5 space-y-4"
                    >
                        <div className="flex justify-between">
                            <div>
                                <h2 className="font-semibold">
                                    Add calendar exception for {technicianId}
                                </h2>
                                <p className="text-xs text-muted-foreground">
                                    Times use the technician&apos;s configured timezone.
                                </p>
                            </div>
                            <button type="button" onClick={() => setExceptionOpen(false)}>
                                <X className="h-4 w-4" />
                            </button>
                        </div>
                        <select
                            className="input w-full"
                            value={exceptionType}
                            onChange={e =>
                                setExceptionType(e.target.value as 'time-off' | 'blocks')
                            }
                        >
                            <option value="time-off">Time off</option>
                            <option value="blocks">Manual schedule block</option>
                        </select>
                        <input
                            required
                            className="input w-full"
                            type="datetime-local"
                            value={exceptionStart}
                            onChange={e => setExceptionStart(e.target.value)}
                        />
                        <input
                            required
                            className="input w-full"
                            type="datetime-local"
                            value={exceptionEnd}
                            onChange={e => setExceptionEnd(e.target.value)}
                        />
                        <input
                            className="input w-full"
                            placeholder="Reason (optional; no placeholder is persisted)"
                            value={exceptionReason}
                            onChange={e => setExceptionReason(e.target.value)}
                        />
                        <button className="btn-primary w-full" type="submit">
                            Save exception
                        </button>
                    </form>
                </div>
            )}
            {scheduleTech && (
                <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
                    <form
                        onSubmit={saveSchedule}
                        className="w-full max-w-xl max-h-[90vh] overflow-auto rounded-lg bg-card border border-border p-5 space-y-3"
                    >
                        <div className="flex justify-between">
                            <div>
                                <h2 className="font-semibold">
                                    Recurring hours · {scheduleTech.display_name || scheduleTech.id}
                                </h2>
                                <p className="text-xs text-muted-foreground">
                                    Comma-separated HH:MM-HH:MM intervals. Blank means off.
                                </p>
                            </div>
                            <button type="button" onClick={() => setScheduleTech(null)}>
                                <X className="h-4 w-4" />
                            </button>
                        </div>
                        {['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'].map(day => (
                            <label
                                key={day}
                                className="grid grid-cols-[5rem_1fr] items-center gap-2 text-sm"
                            >
                                <span className="capitalize">{day}</span>
                                <input
                                    className="input"
                                    placeholder="08:00-12:00, 13:00-17:00"
                                    value={scheduleValues[day] || ''}
                                    onChange={e =>
                                        setScheduleValues(old => ({
                                            ...old,
                                            [day]: e.target.value,
                                        }))
                                    }
                                />
                            </label>
                        ))}
                        <button className="btn-primary w-full" type="submit">
                            Update recurring hours
                        </button>
                    </form>
                </div>
            )}
            {rescheduleAppointment && (
                <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
                    <div className="w-full max-w-lg rounded-lg bg-card border border-border p-5 space-y-3">
                        <div className="flex justify-between">
                            <div>
                                <h2 className="font-semibold">Choose a replacement slot</h2>
                                <p className="text-xs text-muted-foreground">
                                    The backend revalidates capacity before committing.
                                </p>
                            </div>
                            <button onClick={() => setRescheduleAppointment(null)}>
                                <X className="h-4 w-4" />
                            </button>
                        </div>
                        {rescheduleSlots.length === 0 && (
                            <p className="text-sm text-muted-foreground">
                                No replacement capacity is currently available.
                            </p>
                        )}
                        {rescheduleSlots.map(slot => (
                            <button
                                key={slot.slot_token}
                                className="btn-secondary w-full text-left"
                                onClick={() => void applyReschedule(slot)}
                            >
                                {new Date(slot.start).toLocaleString()}–
                                {new Date(slot.end).toLocaleTimeString()} · {slot.technician_id}
                            </button>
                        ))}
                    </div>
                </div>
            )}
        </div>
    );
}
