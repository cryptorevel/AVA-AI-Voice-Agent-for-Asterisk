// @vitest-environment jsdom

import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import axios from 'axios';
import SchedulingSettingsPage from './SchedulingSettingsPage';

vi.mock('axios');
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

const mockedAxios = vi.mocked(axios, true);
const settings = {
    timezone: 'Pacific/Auckland', business_hours: { mon: [['08:00', '16:00']], sun: [] },
    scheduling_enabled: false, minimum_notice_minutes: null, same_day_cutoff: null,
    travel_buffer_before_minutes: null, travel_buffer_after_minutes: null,
    preparation_buffer_minutes: null, configured: {}, readiness: {
        ready: false, missing: ['active_technician'], active_technicians: 0,
        service_assignments: 0, working_intervals: 0, eligible_services: 1, schedulable_technicians: 0,
    },
};

describe('SchedulingSettingsPage', () => {
    beforeEach(() => mockedAxios.get.mockImplementation(async url => {
        if (url === '/api/tools/scheduling/settings') return { data: settings } as never;
        if (url === '/api/tools/scheduling/services') return { data: { services: [{ id: 'generic', display_name: 'Generic', active: true, duration_minutes: 45, auto_bookable: true }] } } as never;
        return { data: { technicians: [] } } as never;
    }));

    it('shows configuration readiness and organization-sourced timezone', async () => {
        render(<MemoryRouter initialEntries={['/admin/settings']}><SchedulingSettingsPage /></MemoryRouter>);
        await waitFor(() => expect(screen.getByDisplayValue('Pacific/Auckland')).toBeTruthy());
        expect(screen.getByText(/Scheduling readiness: Configuration required/i)).toBeTruthy();
        expect(screen.getByText(/active_technician/i)).toBeTruthy();
    });

    it('starts technician management empty without seeding capacity', async () => {
        render(<MemoryRouter initialEntries={['/admin/technicians']}><SchedulingSettingsPage /></MemoryRouter>);
        expect(await screen.findByText(/No technicians configured/i)).toBeTruthy();
        expect(screen.getByText(/Availability remains disabled/i)).toBeTruthy();
    });
});
