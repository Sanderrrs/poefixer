#!/usr/bin/env python3

"""
Perform analysis on the PoE pricing database for various purposes
"""


import re
import sys
import time
import logging
import argparse

import sqlalchemy

import poefixer


DEFAULT_DSN='sqlite:///:memory:'
PRICE_RE = re.compile(r'\~(price|b\/o)\s+(\S+) (\w+)')


def parse_args():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        '-d', '--database-dsn',
        action='store', default=DEFAULT_DSN,
        help='Database connection string for SQLAlchemy')
    parser.add_argument(
        'mode',
        choices=('currency',), # more to come...
        nargs=1,
        action='store', help='Mode to run in.')
    parser.add_argument(
        '-v', '--verbose',
        action='store_true', help='Verbose output')
    parser.add_argument(
        '--debug',
        action='store_true', help='Debugging output')
    return parser.parse_args()

def do_fixer(db, mode, logger):
    assert len(mode) == 1, "Only one mode allowed"
    mode = mode[0]
    if mode == 'currency':
        # Crunch and update currency values
        CurrencyFixer(db, logger).do_currency_fixer()
    else:
        raise ValueError("Expected execution mode, got: " + mode)

class CurrencyFixer:
    db = None
    logger = None

    def __init__(self, db, logger):
        self.db = db
        self.logger = logger

    def parse_note(self, note):
        currencies = {
            "alch": "Orb of Alchemy",
            "alt": "Orb of Alteration",
            "blessed": "Blessed Orb",
            "chance": "Orb of Chance",
            "chaos": "Chaos Orb",
            "chisel": "Cartographer's Chisel",
            "chrom": "Chromatic Orb",
            "divine": "Divine Orb",
            "exa": "Exalted Orb",
            "fuse": "Orb of Fusing",
            "gcp": "Gemcutter's Prism",
            "jew": "Jeweller's Orb",
            "regal": "Regal Orb",
            "regret": "Orb of Regret",
            "scour": "Orb of Scouring",
            "vaal": "Vaal Orb"}

        if note is not None:
            match = PRICE_RE.search(note)
            if match:
                try:
                    (sale_type, amt, currency) = match.groups()
                    if '/' in amt:
                        num, den = amt.split('/', 1)
                        amt = float(num) / float(den)
                    else:
                        amt = float(amt)
                    if  currency in currencies:
                        return (amt, currencies[currency])
                except ValueError as e:
                    # If float() fails it raises ValueError
                    if 'float' in str(e):
                        self.logger.debug("Invalid price: %r" % note)
                    else:
                        raise
        return (None, None)

    def _currency_query(self, block_size, offset):
        """
        Get a query from Item (linked to Stash) that are above the
        last processed time. Return a query that will fetch `block_size`
        rows starting at `offset`.
        """

        Item = poefixer.Item
        processed_time = self.get_last_processed_time()

        query = self.db.session.query(poefixer.Item, poefixer.Stash)
        query = query.add_columns(
            poefixer.Item.id,
            poefixer.Item.api_id,
            poefixer.Item.typeLine,
            poefixer.Item.note,
            poefixer.Item.updated_at,
            poefixer.Stash.stash,
            poefixer.Item.name,
            poefixer.Stash.public)
        query = query.filter(poefixer.Stash.id == poefixer.Item.stash_id)
        query = query.filter(sqlalchemy.or_(
            sqlalchemy.and_(
                poefixer.Item.note != None,
                poefixer.Item.note != ""),
            sqlalchemy.and_(
                poefixer.Stash.stash != None,
                poefixer.Stash.stash != "")))
        query = query.filter(poefixer.Stash.public == True)
        #query = query.filter(sqlalchemy.func.json_contains_path(
        #    poefixer.Item.category, 'all', '$.currency') == 1)
        if processed_time:
            when = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(processed_time))
            self.logger.info("Starting from %s", when)
            query = query.filter(poefixer.Item.updated_at >= processed_time)
        # Tried streaming, but the result is just too large for that.
        query = query.order_by(Item.updated_at).limit(block_size)
        if offset:
            query = query.offset(offset)

        return query

    def _update_currency_pricing(
            self, name, currency, price, sale_time, is_currency):
        """
        Given a currency sale, update our understanding of what currency
        is now worth, and return the value of the sale in Chaos Orbs.
        """

        if is_currency:
            self._update_currency_summary(name, currency, price, sale_time)

        return self._find_value_of(currency, price)

    def _update_currency_summary(self, name, currency, price, sale_time):
        """Update the currency summary table with this new price"""

        query = self.db.session.query(poefixer.CurrencySummary)
        query = query.filter(poefixer.CurrencySummary.from_currency == name)
        query = query.filter(poefixer.CurrencySummary.to_currency == currency)
        do_update = query.one_or_none() is not None

        # This may be DB-specific. Eventually getting it into a
        # pure-SQLAlchemy form would be good...
        weight_query = '''
                SELECT
                    sale2.id,
                    GREATEST(1, (
                        (1.0/GREATEST(1,(:now - sale2.updated_at))) * :unit)) as weight
                FROM sale as sale2'''
        weighted_mean_select = sqlalchemy.sql.text('''
            SELECT
                SUM(sale.sale_amount * wt.weight)/GREATEST(1,SUM(wt.weight)) as mean,
                count(*) as rows
            FROM sale
                INNER JOIN ('''+weight_query+''') as wt
                    ON wt.id = sale.id
            WHERE
                sale.name = :name AND
                sale.sale_currency = :currency''')
        # Our weight unit is how long in seconds we should go before
        # beginning to decay a value. Decay is currently linear
        unit = 24*60*60
        weighted_mean, count_rows = self.db.session.execute(
            weighted_mean_select, {
                'name': name,
                'currency': currency,
                'now': sale_time,
                'unit': unit}).fetchone()

        self.logger.debug(
            "Weighted mean sale of %s for %s %s",
            name, weighted_mean, currency)

        if weighted_mean is None or not count_rows:
            return None

        weighted_stddev_select = sqlalchemy.sql.text('''
            SELECT
                SQRT(
                    SUM(wt.weight * POW(sale.sale_amount - :weighted_mean, 2)) /
                        ((:count_rows * SUM(wt.weight)) / :count_rows)
                ) as weighted_stddev
            FROM sale
                INNER JOIN ('''+weight_query+''') as wt
                    ON wt.id = sale.id
            WHERE
                sale.name = :name AND
                sale.sale_currency = :currency''')
        weighted_stddev, = self.db.session.bind.execute(
            weighted_stddev_select,
            name=name,
            currency=currency,
            count_rows=count_rows,
            weighted_mean=weighted_mean,
            now=sale_time,
            unit=unit).fetchone()
        self.logger.debug(
            "Weighted stddev of sale of %s in %s = %s",
            name, currency, weighted_stddev)
        if weighted_stddev is None:
            return None

        if do_update:
            cmd = sqlalchemy.sql.expression.update(poefixer.CurrencySummary)
            cmd = cmd.where(
                poefixer.CurrencySummary.from_currency == name)
            cmd = cmd.where(
                poefixer.CurrencySummary.to_currency == currency)
            add_values = {}
        else:
            cmd = sqlalchemy.sql.expression.insert(poefixer.CurrencySummary)
            add_values = {
                'from_currency': name,
                'to_currency': currency}
        cmd = cmd.values(
            count=count_rows,
            mean=weighted_mean,
            standard_dev=weighted_stddev, **add_values)
        self.db.session.execute(cmd)

    def _find_value_of(self, name, price):
        """
        Return the best current understanding of the value of the
        named currency, in chaos, multiplied by the numeric `price`.

        Our primitive way of doing this for now is to say that the
        largest number of values wins, presuming that that means
        the most stable sample, and we only try to follow the exchange
        to two levels down. Thus, we look for `X -> chaos` and
        `X -> Y -> chaos` and take whichever of those has the
        highest number of witnessed sales (the number of sales of
        `X -> Y -> chaos` being `min(count(X->Y), count(Y->chaos))`

        If all of that fails, we look for transactions going the other
        way (`chaos -> X`). This is less reliable, since it's a
        supply vs. demand side order, but if it's all we have, we
        roll with it.
        """

        if name == 'Chaos Orb':
            return price

        from_currency_field = poefixer.CurrencySummary.from_currency
        to_currency_field = poefixer.CurrencySummary.to_currency

        query = self.db.session.query(poefixer.CurrencySummary)
        query = query.filter(from_currency_field == name)
        query = query.order_by(poefixer.CurrencySummary.count.desc())
        high_score = None
        conversion = None
        for row in query.all():
            target = row.to_currency
            if target == 'Chaos Orb':
                if high_score and row.count >= high_score:
                    self.logger.info(
                        "Conversion discovered %s -> Chaos = %s",
                        name, row.mean)
                    high_score = row.count
                    conversion = row.mean
                break
            query2 = self.db.session.query(poefixer.CurrencySummary)
            query2 = query2.filter(from_currency_field == target)
            query2 = query2.filter(to_currency_field == 'Chaos Orb')
            row2 = query2.one_or_none()
            if row2:
                score = min(row.count, row2.count)
                if (not high_score) or score > high_score:
                    high_score = score
                    conversion = row.mean * row2.mean
                    self.logger.info(
                        "Conversion discovered %s -> %s (%s) -> Chaos (%s) = %s",
                        name, row2.from_currency, row.mean,
                        row2.mean, conversion)

        if high_score:
            return conversion * price
        else:
            query = self.db.session.query(poefixer.CurrencySummary)
            query = query.filter(from_currency_field == 'Chaos Orb')
            query = query.filter(to_currency_field == name)
            row = query.one_or_none()
            if row:
                return 1.0 / row.mean

        return None

    def _process_sale(self, row):
        if not (
                (row.Item.note and row.Item.note.startswith('~')) or
                row.Stash.stash.startswith('~')):
            self.logger.debug("No sale")
            return
        is_currency = 'currency' in row.Item.category
        if is_currency:
            name = row.Item.typeLine
        else:
            name = row.Item.name + " " + row.Item.typeLine
        pricing = row.Item.note
        stash_pricing = row.Stash.stash
        stash_price, stash_currency = self.parse_note(stash_pricing)
        price, currency = self.parse_note(pricing)
        if price is None:
            # No item price, so fall back to stash
            price, currency = (stash_price, stash_currency)
        if price is None or price == 0:
            self.logger.debug("No sale")
            return
        self.logger.debug(
            "%s%sfor sale for %s %s" % (
                name,
                ("(currency) " if is_currency else ""),
                price, currency))
        existing = self.db.session.query(poefixer.Sale).filter(
            poefixer.Sale.item_id == row.Item.id).one_or_none()

        if not existing:
            existing = poefixer.Sale(
                item_id=row.Item.id,
                item_api_id=row.Item.api_id,
                name=name,
                is_currency=is_currency,
                sale_currency=currency,
                sale_amount=price,
                sale_amount_chaos=None,
                created_at=int(time.time()),
                item_updated_at=row.Item.updated_at,
                updated_at=int(time.time()))
        else:
            existing.sale_currency = currency
            existing.sale_amount = price
            existing.sale_amount_chaos = None
            existing.item_updated_at = row.Item.updated_at
            existing.updated_at = int(time.time())

        # Add it so we can re-calc values...
        self.db.session.add(existing)
        self.db.session.flush()

        amount_chaos = self._update_currency_pricing(
            name, currency, price, row.Item.updated_at, is_currency)

        if amount_chaos is not None:
            self.logger.info(
                "Found chaos value of %s -> %s %s = %s",
                name, price, currency, amount_chaos)

            existing.sale_amount_chaos = amount_chaos
            self.db.session.merge(existing)

    def get_last_processed_time(self):
        """
        Get the item update time relevant to the most recent sale
        record.
        """

        query = self.db.session.query(poefixer.Sale)
        query = query.order_by(poefixer.Sale.item_updated_at.desc()).limit(1)
        result = query.one_or_none()
        if result:
            reference_time = result.item_updated_at
            when = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(reference_time))
            self.logger.debug(
                "Last processed sale for item: %s(%s)",
                result.item_id, when)
            return reference_time
        return None


    def do_currency_fixer(self):
        """Process all of the currency data we've seen to date."""

        offset = 0
        count = 0
        todo = True
        block_size = 1000 # Number of rows per block

        def create_table(table, name):
            try:
                table.__table__.create(bind=self.db.session.bind)
            except sqlalchemy.exc.InternalError as e:
                if 'already exists' not in str(e):
                    raise
                self.logger.info("%s table already exists.", name)
            else:
                self.logger.info("%s table created.", name)

        create_table(poefixer.Sale, "Sale")
        create_table(poefixer.CurrencySummary, "Currency Summary")

        while todo:
            query = self._currency_query(block_size, offset)

            # Stashes are named with a conventional pricing descriptor and
            # items can have a note in the same format. The price of an item
            # is the item price with the stash price as a fallback.
            count = 0
            for row in query.all():
                max_id = row.Item.id
                count += 1
                self.logger.debug("Row in %s" % row.Item.id)
                if count % 100 == 0:
                    self.logger.info("%s rows in..." % (count + offset))
                self._process_sale(row)

            todo = count == block_size
            offset += count
            self.db.session.commit()

if __name__ == '__main__':
    options = parse_args()
    echo = False

    logger = logging.getLogger('poefixer')
    if options.debug:
        loglevel = 'DEBUG'
        echo = True
    elif options.verbose:
        loglevel = 'INFO'
    else:
        loglevel = 'WARNING'
    logger.setLevel(loglevel)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s:%(name)s:%(levelname)s: %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    logger.debug("Set logging level: %s" % loglevel)

    db = poefixer.PoeDb(
        db_connect=options.database_dsn, logger=logger, echo=echo)
    db.session.bind.execution_options(stream_results=True)
    do_fixer(db, options.mode, logger)


# vim: et:sw=4:sts=4:ai:
