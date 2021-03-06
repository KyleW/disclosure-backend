"""
Command to download and load California campaign finance data from Netfile.

See https://netfile.com/Filer/Content/docs/cal_format_201.pdf for documentation.
"""

import datetime
import warnings
from dateutil.parser import parse as date_parse
from itertools import izip_longest
from numbers import Number
from optparse import make_option

import numpy as np
import pandas as pd

from django.core.management.base import CommandError
from django.db import transaction

from ... import models
from ballot.models import Ballot
from ballot.models import Candidate, Office, OfficeElection, Party
from ballot.models import Referendum, ReferendumSelection
from locality.models import City, State, ZipCode
from netfile_raw.management.commands import downloadnetfilerawdata


class SkipForm(Exception):
    """Exception to indicate when form data should be skipped."""
    pass


def isnan(val):
    return isinstance(val, Number) and np.isnan(val)


def isnone(val):
    return val is None or val == 'None'


def clean_name(str):
    """BEN CIP => Ben Cip"""
    if str is None or str == '':
        return str
    return ' '.join([n[0].upper() + n[1:].lower()
                     for n in str.strip().split(' ')
                     if n != ''])


def clean_city(city):
    """ALAMEDA, CA => Alameda"""
    if city is None:
        return city
    city = city.strip().split(',')[0].strip()
    return clean_name(city)


def clean_state(state):
    """Ca => CA"""
    if state is None:
        return state
    state = state.strip().upper()
    if len(state) != 2:
        print("WARNING: strange state: %s" % state)
    return state


def clean_zip(zip_code):
    """92110-0123, CA => 92110"""
    if zip_code is None:
        return zip_code
    zip_code = str(zip_code).strip().upper()  # foreign zips
    zip_code = zip_code.split(',')[0].strip()
    zip_code = zip_code.split('-')[0].strip()
    if len(zip_code) != 5:
        print("WARNING: strange zip code: %s" % zip_code)
    return zip_code


def parse_benefactor(row, verbosity=1):
    # Benefactor info
    bf_state, _ = State.objects.get_or_create(
        short_name=clean_state(row.get('tran_ST')) or 'Unknown-State')
    bf_city, _ = City.objects.get_or_create(
        name=clean_city(row.get('tran_City')) or 'Unknown-City',
        state=bf_state)
    bf_zip_code, _ = ZipCode.objects.get_or_create(
        short_name=clean_zip(str(row.get('tran_Zip4') or '')) or 'Unknown-Zip',
        state=bf_state)

    # Make sure row type is of the known types
    assert row['entity_Cd'] in ('IND', 'OTH', 'SCC', 'COM', 'PTY', 'CRT')

    if row['entity_Cd'] == 'IND':  # individual
        employer_name = clean_name(row.get('tran_Emp'))
        if employer_name:
            employer, _ = models.Employer.objects.get_or_create(name=employer_name)
        else:
            employer = None
        raw_name = clean_name(row.get('tran_NamF')) or ''
        first_name = raw_name.split(' ')[0]
        middle_name = raw_name[len(first_name):].strip()
        benefactor, _ = models.PersonBenefactor.objects.get_or_create(
            first_name=first_name, middle_name=middle_name,
            last_name=clean_name(row['tran_NamL']),
            employer=employer,
            city=bf_city,
            state=bf_state,
            zip_code=bf_zip_code,
            benefactor_locality=bf_city)
        benefactor.occupation = clean_name(row.get('tran_Occ'))  # Not reliable
        benefactor.save()

    elif row['entity_Cd'] == 'OTH':  # Commerial benefactor or Other
        benefactor, _ = models.OtherBenefactor.objects \
            .get_or_create(name=clean_name(row['tran_NamL']))
        benefactor.benefactor_locality = bf_city
        benefactor.save()

    elif row['entity_Cd'] in ['SCC', 'COM', 'CRT']:
        # committee or commitee receipt, for 460D
        # Get by name
        benefactor = get_committee_benefactor(row)
        benefactor.benefactor_type = benefactor.type
        benefactor.city = bf_city
        benefactor.state = bf_state
        benefactor.zip_code = bf_zip_code
        benefactor.locality = benefactor.benefactor_locality = bf_city
        benefactor.save()

        # Now validate that there aren't dups
        queryset = models.CommitteeBenefactor.objects.filter(
            name=benefactor.name, filer_id=benefactor.filer_id,
            locality=bf_city, type=benefactor.type)
        assert queryset.count() == 1, "Avoid duplicate committees"

    elif row['entity_Cd'] in ['PTY']:
        name = clean_name(row['tran_NamL'])
        party = parse_party_from_name(name)
        benefactor, _ = models.PartyBenefactor.objects \
            .get_or_create(name=name, party=party)

        benefactor.city = bf_city
        benefactor.state = bf_state
        benefactor.zip_code = bf_zip_code
        benefactor.locality = benefactor.benefactor_locality = bf_city
        benefactor.save()

    return benefactor, bf_zip_code


def parse_party_from_name(committee_name):
    known_parties = dict(
        Republican=('Republican',),
        Democrat=('Democrat', 'Democratic'))

    # San Diego County Democratic Party
    for key, val in known_parties.items():
        if np.any([s.lower() in committee_name.lower() for s in val]):
            return Party.objects.get_or_create(name=key)[0]
    return Party.objects.get_or_create(name='Unknown')[0]


def parse_form_and_report_period(row, form, locality, verbosity=1):
    # Create/get the relevant form objects.
    form_name = form['form_name']
    form_type = form['form_type']
    if len(form_type) == 1:
        form_type = '460' + form_type

    form, _ = models.Form.objects.get_or_create(
        name=form_name, text_id=form_type)

    report_date = date_parse(row['tran_Date'])
    reporting_periods = models.ReportingPeriod.objects.filter(
        form=form, locality=locality,
        period_start__lte=report_date, period_end__gte=report_date)

    if len(reporting_periods) == 0:
        raise Exception(("You must add a reporting period for form '%s', "
                        "locality '%s', containing date %s") % (
                            form, locality, report_date))

    elif len(reporting_periods) > 1:
        raise Exception("More than one reporting period matches; please "
                        "review.")

    # datetime.datetime(report_date.year, 1, 1),
    #     period_end=datetime.datetime(report_date.year, 12, 31))

    return reporting_periods[0]


def parse_beneficiary(row, agency, verbosity=1):
    # Parse and save the beneficiary, contribution.
    assert agency is not None, "Agency should be set."

    state, _ = State.objects.get_or_create(short_name='CA')
    if state.name is None or state.name == '':
        state.name = 'California'
        state.save()
    locality, _ = City.objects.get_or_create(
        name=agency['name'],
        state=state)
    locality.short_name = agency['shortcut']
    locality.save()

    beneficiary = get_committee_beneficiary(row)
    beneficiary.locality = locality
    beneficiary.name = clean_name(row['filerName'])
    beneficiary.type = 'PF'  # ok
    # beneficiary.address = '?'  # TODO: fix
    beneficiary.save()
    return beneficiary


def parse_candidate_and_office(row, verbosity=1):
    import re
    res = (
        # David Alvarez for Mayor 2014
        '^(?P<name>.*?)\s+for\s+(?P<office>.*?)(?P<year>[0-9]+).*$',
        # Scott Sanborn City Council 2016
        '^(?P<name>.*? .*?)\s+(?P<office>.*?)(?P<year>[0-9]+).*$',
    )

    # Either find a match, or raise an error.
    for re_str in res:
        matches = re.match(re_str, clean_name(row['filerName']))
        if matches is not None:
            matches = matches.groupdict()
            break
    if matches is None:
        raise ValueError("Could not parse a Candidate.")

    name_parts = matches['name'].strip().split(" ")
    last_name = name_parts[-1]
    first_name = " ".join(name_parts[:-1]).strip() or None
    office = matches['office'].strip()
    return last_name, first_name, office


def parse_candidate_info(row, ballot, verbosity=1):
    # Try to get candidate name from beneficiary name.
    last_name, first_name, office_name = parse_candidate_and_office(
        row, verbosity=verbosity)
    candidate_office, _ = Office.objects.get_or_create(
        name=office_name, locality=ballot.locality)
    office_election, _ = OfficeElection.objects.get_or_create(
        office=candidate_office, ballot=ballot)
    candidate, _ = Candidate.objects.get_or_create(
        first_name=first_name, last_name=last_name,
        office_election=office_election)
    return candidate


def parse_referendum_info(row, ballot, verbosity=1):
    referendum, _ = Referendum.objects.get_or_create(
        title='Unknown', ballot=ballot)
    selection, _ = ReferendumSelection.objects.get_or_create(
        ballot_item=referendum,
        in_favor=True)
    return selection


def parse_ballot_info(row, locality, verbosity=1):
    ballot = Ballot.from_date(date=date_parse(row['tran_Date']),
                              locality=locality)

    # Figure out beneficiary from past entries.
    past_money = models.IndependentMoney.objects.filter(
        beneficiary__name=clean_name(row['filerName']),
        beneficiary__ballot_item_selection__ballot_item__ballot=ballot)
    if past_money.count() > 0:
        # Figure it out from past contributions.
        ballot_item_selection = past_money[0] \
            .beneficiary.ballot_item_selection
    else:
        # Figure out beneficiary from item text.
        try:
            ballot_item_selection = parse_candidate_info(
                row, ballot=ballot, verbosity=verbosity)
        except ValueError:
            ballot_item_selection = parse_referendum_info(
                row, ballot=ballot, verbosity=verbosity)

    return ballot_item_selection, True


def get_committee_benefactor(row):
    """Utility function to identify a committee benefactor.
    """
    # $HACK; fields differ for contributions vs. expenditures!
    if row.get('form_Type') == 'D':
        filer_id = row.get('filerId') or row.get('filerLocalId') or row.get('filerStateId')
        name = clean_name(row.get('filerName'))
    else:
        filer_id = clean_filer_id(row.get('cmte_Id'))
        name = clean_name(row.get('cand_NamL') or row.get('tran_NamL') or row.get(''))

    benefactor = None

    if filer_id is not None:
        # By ID
        benefactors = models.CommitteeBenefactor.objects.filter(
            filer_id=filer_id)
        if benefactors.count() > 0:
            # Matched on filer_id
            benefactor = benefactors[0]
            benefactor.name = name

    if benefactor is None:
        # By name
        benefactor, _ = models.CommitteeBenefactor.objects.get_or_create(
            name=name)
        benefactor.filer_id = filer_id or benefactor.filer_id

    # CC=candidate-controlled, pf=primarily, ic=general purpose, BM=ballot measure
    if row['form_Type'] in ['F496P3']:  # TODO: take form info OUT of code
        default_type = 'IC'
    else:
        default_type = 'PF'
    benefactor.type = benefactor.type or default_type  # TODO: fix

    return benefactor


def get_committee_beneficiary(row):
    """Utility function to identify a committee beneficiary.
    """
    filer_id = row.get('filerId') or row.get('filerLocalId') or row.get('filerStateId')
    if filer_id is None:
        raise Exception('Did the Netfile schema change again??')
    elif '-' in filer_id:
        filer_id = '-'.join(filer_id.split('-')[1:])
    elif filer_id == 'pending':
        filer_id = None
    else:
        filer_id = filer_id

    return models.Beneficiary.objects.get_or_create(
        name=clean_name(row.get('filerName')), filer_id=filer_id)[0]


def clean_filer_id(filer_id):
    """Utility function to scrub committee IDs."""
    if isinstance(filer_id, Number):
        filer_id = str(int(filer_id))
    elif filer_id and np.any([filer_id.startswith(c) for c in ('C', '#')]):
        filer_id = filer_id[1:]

    # Filer ID should be an int.
    try:
        return str(int(filer_id))
    except:
        return None


@transaction.atomic
def load_form_row(row, agency, reporting_period, force=False, verbosity=1):  # noqa
    """ Loads an individual row from Form 460 Schedule A. # noqa
    This is where most of the magic happens!

    Returns IndependentMoney instance, and
    True if data was loaded into the database,
    False if skipped b/c data already is there.

    Some metadata:
    ^ calculated_Amount: None, -294.84,-200.0,50.0,99.0,100.0
    x calculated_Date: None, 2015-01-07T00:00:00.0000000-08
    ^ cmte_Id        : Committee ID # (If [COM|RCP] &, nan,#890268,1244975,1264568,12
    ^ entity_Cd      : Contributor Type of Entity (In, COM,IND,OTH,SCC
    ^ filerId        : None, COA-113952,COA-113968,COA-1139
    ^ filerName      : None, Better Transportation for Alam
    x filingId       : None, 155104080,155521798,155524068,
    x intr_City      : Intermediary City, nan,Alameda
    x intr_NamL      : Intermediary Last Name, nan,Wilma Chan For Supervisor
    x intr_ST        : Intermediary State, nan,CA
    x intr_Zip4      : Intermediary Zip, nan,94501
    x netFileKey     : None, 00bab3072253498da017a4e60105ea
    ^ tran_Amt1      : Transaction Amount, -200.0,-294.84,100.0,1000.0,10
    ^ tran_Amt2      : Cumulative Year-To-Date, -6867.12,0.0,100.0,1000.0,101.
    tran_City      : Transaction Entity's City, Alameda,Alamo,Arcata,Atherton,
    tran_Date      : Transaction Date, 2015-01-07T00:00:00.0000000-08
    tran_Date1     : Transaction Date (if a range), 2013-10-03T00:00:00.0000000-07
    tran_Emp       : Transaction Entity's Employer, nan,ABCO Wire & Metal Products
    tran_Id        : Transaction ID # (not necessar, 1DOCUB61ZGyZ,21mtbGMusZdR,22Lp
    tran_NamF      : Transaction Entity's First Nam, nan,Albert,Alice,Andrew,Andy,A
    tran_NamL      : Transaction Entity's Last Name, ABC Security  Service Inc,ACME
    tran_Occ       : Transaction Entity's Occupatio, nan,Actuary,Acupuncturist,Acup
    tran_ST        : None, CA,MO,TX
    tran_Type      : Transaction Type (T=Third Part, nan,R,X
    tran_Zip4      : Transaction Entity's Zip Code, 63105,75702,75711,90036,90040,
    """

    if not force:
        try:
            # Get old money. If we have it, don't do anything--fast!!
            money = models.IndependentMoney.objects.get(
                source='NF',
                source_xact_id=row['netFileKey'])

            assert money.amount == float(row['tran_Amt1']), \
                "%s != %s" % (money.amount, float(row['tran_Amt1']))
            assert money.cumulative_amount == (float(row.get('tran_Amt2', 0)) or None), \
                "%s != %s" % (money.cumulative_amount, float(row.get('tran_Amt2', 0)) or None)
            assert str(date_parse(row['tran_Date'])).startswith(str(money.report_date)), \
                "%s != %s" % (money.report_date, date_parse(row['tran_Date']))
            if verbosity:
                print("Skipping existing [%s] row, %s/%s" % (
                    reporting_period.form.name, money.source, money.source_xact_id))

            return money, False  # Did not load.

        except models.IndependentMoney.DoesNotExist:
            pass

    benefactor, bf_zip_code = parse_benefactor(
        row, verbosity=verbosity)
    beneficiary = parse_beneficiary(
        row, agency=agency, verbosity=verbosity)
    beneficiary.ballot_item_selection, beneficiary.support = parse_ballot_info(
        row, locality=beneficiary.locality, verbosity=verbosity)
    beneficiary.save()

    # Now we have all the parts. Create and save it.
    money = models.IndependentMoney(
        source='NF',
        source_xact_id=row['netFileKey'],
        filing_id=row.get('filingId'),
        amount=float(row['tran_Amt1']),
        cumulative_amount=float(row.get('tran_Amt2', 0)) or None,
        report_date=date_parse(row['tran_Date']),
        reporting_period=reporting_period,
        benefactor_zip=bf_zip_code,
        benefactor=benefactor,
        beneficiary=beneficiary)
    money.save()

    if verbosity:
        print(str(money))

    return money, True


@transaction.atomic
def load_form460d_row(row, agency, reporting_period, force=False, verbosity=1):  # noqa
    """ Loads an individual row from Form 460 Schedule A. # noqa
    This is where most of the magic happens!

    Some metadata:
    agency_shortcut                                                    CSD
    amountType                                                           0
    tran_Id                                                           D748
    filingStartDate                      2015-01-01T00:00:00.0000000-08:00
    tran_Amt1                                                        900.0
    filingId                                                     155100606
    sup_Opp_Cd                                                           S
    memo_Code                                                        False
    filingEndDate                        2015-05-12T00:00:00.0000000-07:00
    transactionType                                                      5
    calculated_Amount                                                  900
    intr_Self                                                        False
    tran_Amt2                                                        900.0
    xref_Match                                                       False
    tran_Code                                                          MON
    form_Type                                                            D
    tran_Date                            2015-05-11T00:00:00.0000000-07:00
    latitude                                                             0
    longitude                                                            0
    rec_Type                                                          EXPN
    filerLocalId                                                  CSD-5244
    calculated_Date                      2015-05-11T00:00:00.0000000-07:00
    filerName            Jan Goldsmith (As City Attorney, I do not have...
    filerStateId                                                   pending
    netFileKey                            aa3d5a02ccc646bd8187a4990078e1ba
    cand_NamL                         Republican Party of San Diego County
    tran_Self                                                        False
    entity_Cd                                                          COM
    """

    money, loaded = load_form_row(
        row=row, agency=agency, reporting_period=reporting_period,
        force=force, verbosity=verbosity)

    # We can load some info about support/oppose.
    if loaded:
        beneficiary = money.beneficiary
        support = row.get('sup_Opp_Cd')
        beneficiary.support = (support == 'S')
        if beneficiary.ballot_item_selection is None:
            beneficiary.ballot_item_selection, _ = parse_ballot_info(
                row, locality=money.beneficiary.locality, verbosity=verbosity)
        beneficiary.save()
        print(beneficiary)
    return loaded


def minimize_row(raw_row):
    """Return a row with all 'None' entries removed."""
    minimal_row = raw_row.copy()

    # remove useless columns for easier exploring
    for col in minimal_row.keys():
        if isnone(minimal_row[col]) or isnan(minimal_row[col]):
            del minimal_row[col]
    return minimal_row


def grouper(n, iterable, fillvalue=None):
    """
    Group an interable into chunks of size n.
    """
    args = [iter(iterable)] * n
    return izip_longest(fillvalue=fillvalue, *args)


def find_unloaded_rows(data, skip_rate=100, force=False, verbosity=1):
    """
    Get a list of 0 to skip_rate rows of transactions not in the DB.
    """
    xact_keys = data['netFileKey']
    for xacts in grouper(skip_rate, xact_keys):
        vals = [v['source_xact_id'] for v in models.IndependentMoney.objects.filter(
            source='NF', source_xact_id__in=xacts).values('source_xact_id')]
        if verbosity:
            for val in vals:
                print("Skipping NF/%s" % val)

        if force:
            yield set(xacts)
        else:
            yield set(xacts) - set(vals)  # missing set


def delete_form_data(reporting_period, verbosity=1):
    rows_to_delete = models.IndependentMoney.objects.filter(
        reporting_period=reporting_period)
    if verbosity > 0:
        print("Deleting %d rows of irrelevant %s data from between %s and %s." % (
            rows_to_delete.count(), reporting_period.form,
            reporting_period.period_start, reporting_period.period_end))
    rows_to_delete.delete()


def load_form_data(data, agency_fn, form_name, form_type=None,
                   force=False, verbosity=1):
    """
    """
    if form_type is not None:
        data = data[data['form_Type'] == form_type]

    if verbosity > 0:
        print("Attempting to load %d rows of %s data." % (len(data), form_name))

    # Parse out the contributor information.
    reporting_period = None  # Set on first valid row.
    error_rows = []
    xact_key_generator = find_unloaded_rows(data, force=force, verbosity=verbosity)
    xact_keys = []
    for ri, (_, raw_row) in enumerate(data.T.iteritems()):
        # Quickly get near an unloaded row.
        while not xact_keys and xact_keys is not None:
            xact_keys = next(xact_key_generator)
        if xact_keys is None or raw_row['netFileKey'] not in xact_keys:
            continue
        xact_keys = xact_keys - set([raw_row['netFileKey']])

        minimal_row = minimize_row(raw_row)

        assert minimal_row.get('rec_Type') in ('RCPT', 'S497', 'EXPN')
        # print minimal_row

        try:
            agency = agency_fn(minimal_row)

            if reporting_period is None:
                # Query form period.
                form = dict(form_name=form_name, form_type=form_type)
                locality = City.objects.get(short_name=agency['shortcut'])
                reporting_period = parse_form_and_report_period(
                    minimal_row, form, locality=locality, verbosity=verbosity)

                # Verify that the data from this form are relevant.
                now = datetime.datetime.date(datetime.datetime.now())
                time_to_skip = (now < reporting_period.period_start or
                                now > reporting_period.period_end)

                if not reporting_period.permanent and time_to_skip:
                    # No longer relevant. Delete any existing data, and skip this.
                    delete_form_data(reporting_period)
                    raise SkipForm()

            # 460D data is different...
            if form_type == 'D':
                minimal_row['entity_Cd'] = minimal_row.get('entity_Cd', 'CRT')
                load_form460d_row(
                    minimal_row, agency=agency, reporting_period=reporting_period,
                    force=force, verbosity=verbosity)
            else:
                load_form_row(
                    minimal_row, agency=agency, reporting_period=reporting_period,
                    force=force, verbosity=verbosity)
        except Exception as ex:
            error_rows.append((ri, raw_row, minimal_row, ex))
            # TODO: Store errors, for review later.
            raise

    return error_rows


custom_options = (
    make_option(
        "--forms",
        action="store",
        dest="forms",
        default=None,
        help="Form types to upload (comma-separated list; "
             "choices=('A', 'C', 'F497P1', 'F496P3')"
    ),
)


class Command(downloadnetfilerawdata.Command):
    help = 'Download and load the netfile raw data into the clean database.'
    option_list = downloadnetfilerawdata.Command.option_list + custom_options

    FORM_TYPES = [
        {'form_name': "Form 460 Schedule A", 'form_type': 'A'},
        {'form_name': "Form 460 Schedule C", 'form_type': 'C'},
        {'form_name': "Form 460 Schedule D", 'form_type': 'D'},
        {'form_name': "Form 497", 'form_type': 'F497P1'}
    ]

    def handle(self, *args, **options):
        # Resolve human-entered forms.
        ALL_FORM_TYPES = [f['form_type'] for f in self.FORM_TYPES]
        user_form_types = options['forms'].split(',') if options['forms'] else ALL_FORM_TYPES
        self.forms = []
        for form_type in user_form_types:
            if form_type not in ALL_FORM_TYPES:
                raise CommandError("Unknown form type '%s'  ; choose from %s" % (
                    form_type, ALL_FORM_TYPES))
            self.forms += [f for f in self.FORM_TYPES if f['form_type'] == form_type]

        super(Command, self).handle(*args, **options)

    def load(self):
        """
        According to:
        https://github.com/caciviclab/caciviclab.github.io/wiki/Campaign-Finance

        To get a comprehensive total of the amount of money raised for
        any ballot measure, you must include:
        * Form 460, Schedules A (monetary contributions) and
            C (nonmonetary contributions), for all Primarily Formed and
            Candidate Controlled committees
        * Form 497, beginning from the ending period of the last Form 460 that
            was filed, for all Primarily Formed and
            Candidate Controlled committees
        * Form 496, only expenses in support, only from committees that
            are NOT the Primarily Formed or Candidate Controlled committees,
            and only those beginning from the ending period of the
            last Form 460 that was filed
        """
        if self.verbosity:
            self.header("Loading disclosure data into database.")

        self.data = pd.read_csv(self.combined_csv_path)
        if self.verbosity:
            self.header("There are %d rows in %s" % (
                        len(self.data), self.combined_csv_path))

        # Check for any potentially missing data.
        form_types = self.data['form_Type'].unique()
        if len({None, np.nan} - set(form_types)) != 2:
            warnings.warn("Some data don't have form_Type set.")

        for form_info in self.forms:
            try:
                error_rows = load_form_data(
                    data=self.data, verbosity=self.verbosity, force=self.force,
                    agency_fn=lambda row: self.get_agency(row['agency_shortcut']),
                    **form_info)

                # Report errors  TODO: push to the database.
                if len(error_rows) > 0:
                    print("Encountered %d errors; debug!" % len(error_rows))
                    print("Errors:\n%s" % ','.join([e[-1] for e in error_rows]))
            except SkipForm:
                if self.verbosity > 0:
                    print("Skipping irrelevant form data from %s" % form_info)
                continue

    def get_agency(self, agency_shortcut):
        agency_matches = filter(lambda a: a['shortcut'] == agency_shortcut,
                                self.agencies_metadata)
        if len(agency_matches) == 0:
            return None
        agency = agency_matches[0]

        # Scrub it
        if agency['name'].endswith(', City of'):
            agency['name'] = agency['name'][:-9]

        return agency
