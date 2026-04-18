"""
Smart Budget Planner & AI Financial Advisor
A comprehensive web-based financial management application

Author: AI Assistant
Stack: Flask + MongoDB + HTML/CSS/JS + Chart.js
"""

from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
import calendar
import os
import ssl

from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-here-change-in-production')
app.config['MONGODB_URI'] = os.environ.get('MONGODB_URI', '')
app.config['MONGODB_DB_NAME'] = os.environ.get('MONGODB_DB_NAME', 'budget_db')

mongo_client = None
mongo_db = None


@app.context_processor
def inject_datetime():
    """Make datetime available in all templates."""
    return {'datetime': datetime}


def init_mongo():
    """Initialize MongoDB connection and indexes."""
    global mongo_client, mongo_db
    mongodb_uri = app.config.get('MONGODB_URI', '').strip()
    if not mongodb_uri:
        print('MONGODB_URI not found in environment. MongoDB is required.')
        mongo_client = None
        mongo_db = None
        return

    try:
        mongo_client = MongoClient(mongodb_uri, serverSelectionTimeoutMS=5000, tlsAllowInvalidCertificates=True)
        mongo_client.admin.command('ping')

        db_name = app.config.get('MONGODB_DB_NAME', 'budget_db').strip() or 'budget_db'
        mongo_db = mongo_client[db_name]

        mongo_db.users.create_index('username', unique=True)
        mongo_db.users.create_index('email', unique=True, sparse=True)
        mongo_db.transactions.create_index([('user_id', 1), ('date', -1)])
        mongo_db.savings_goals.create_index([('user_id', 1), ('created_at', -1)])

        print('MongoDB connected successfully.')
    except PyMongoError as error:
        print(f'MongoDB connection failed: {error}')
        mongo_client = None
        mongo_db = None


def mongo_ready():
    return mongo_db is not None


def object_id(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def format_created_at(value):
    if isinstance(value, datetime):
        return value.strftime('%b %d, %Y')
    return str(value) if value else ''


def user_doc_to_view(doc):
    if not doc:
        return None
    return {
        'id': str(doc['_id']),
        'username': doc['username'],
        'password': doc['password'],
        'email': doc.get('email') or '',
        'monthly_salary': float(doc.get('monthly_salary') or 0),
        'savings_goal': float(doc.get('savings_goal') or 0),
        'created_at': format_created_at(doc.get('created_at')),
    }


def transaction_doc_to_view(doc):
    if not doc:
        return None

    date_value = doc.get('date')
    if isinstance(date_value, datetime):
        date_value = date_value.strftime('%Y-%m-%d')
    elif date_value is None:
        date_value = ''

    return {
        'id': str(doc['_id']),
        'user_id': doc['user_id'],
        'type': doc['type'],
        'category': doc['category'],
        'amount': float(doc.get('amount') or 0),
        'description': doc.get('description') or '',
        'date': date_value,
        'created_at': doc.get('created_at'),
    }


def goal_doc_to_view(doc):
    if not doc:
        return None
    return {
        'id': str(doc['_id']),
        'user_id': doc['user_id'],
        'name': doc['name'],
        'target_amount': float(doc['target_amount']),
        'current_amount': float(doc.get('current_amount') or 0),
        'deadline': doc.get('deadline') or '',
        'created_at': doc.get('created_at'),
    }


# Authentication Decorator
def login_required(view):
    """Decorator to require login for routes"""

    @wraps(view)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please login to access this page', 'warning')
            return redirect(url_for('login'))
        if not mongo_ready():
            flash('Database is unavailable. Check MONGODB_URI in .env', 'danger')
            return redirect(url_for('login'))
        user_oid = object_id(session['user_id'])
        if user_oid is None or mongo_db.users.find_one({'_id': user_oid}) is None:
            session.clear()
            flash('Session expired. Please login again.', 'warning')
            return redirect(url_for('login'))
        return view(*args, **kwargs)

    return decorated_function


def get_user_data(user_id):
    """Get user data from database"""
    oid = object_id(user_id)
    if oid is None:
        return None
    doc = mongo_db.users.find_one({'_id': oid})
    return user_doc_to_view(doc)


def get_user_filter(user_id):
    """Return a filter matching the current user by string or ObjectId."""
    oid = object_id(user_id)
    if oid is None:
        return {'user_id': user_id}
    return {'user_id': {'$in': [oid, user_id]}}


def get_transactions(user_id, limit=None, date_from=None, date_to=None, category=None, search=None):
    """Get transactions with optional filters"""
    flt = get_user_filter(user_id)

    date_range = {}
    if date_from:
        date_range['$gte'] = date_from
    if date_to:
        date_range['$lte'] = date_to
    if date_range:
        flt['date'] = date_range

    if category:
        flt['category'] = category

    if search:
        pattern = search.strip()
        if pattern:
            flt['$or'] = [
                {'description': {'$regex': pattern, '$options': 'i'}},
                {'category': {'$regex': pattern, '$options': 'i'}},
            ]

    cursor = (
        mongo_db.transactions.find(flt)
        .sort([('date', -1), ('created_at', -1)])
    )
    if limit:
        cursor = cursor.limit(limit)

    return [transaction_doc_to_view(d) for d in cursor]


def get_financial_summary(user_id):
    """Calculate financial summary for user"""
    txs = list(mongo_db.transactions.find(get_user_filter(user_id)))

    total_income = sum(t['amount'] for t in txs if t['type'] == 'income')
    total_expenses = sum(t['amount'] for t in txs if t['type'] == 'expense')

    current_month = datetime.now().strftime('%Y-%m')
    monthly_expenses = sum(
        t['amount'] for t in txs
        if t['type'] == 'expense' and isinstance(t.get('date'), str) and t['date'][:7] == current_month
    )

    category_totals = defaultdict(float)
    for t in txs:
        if t['type'] == 'expense':
            category_totals[t['category']] += float(t['amount'])
    category_expenses = [
        {'category': k, 'total': v}
        for k, v in sorted(category_totals.items(), key=lambda item: item[1], reverse=True)
    ]

    week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    weekly_map = defaultdict(float)
    for t in txs:
        if t['type'] == 'expense' and t.get('date') and t['date'] >= week_ago:
            weekly_map[t['date']] += float(t['amount'])
    weekly_expenses = [
        {'date': d, 'total': weekly_map[d]}
        for d in sorted(weekly_map.keys())
    ]

    user = get_user_data(user_id)
    monthly_salary = user['monthly_salary'] if user else 0

    today = datetime.now()
    last_day_of_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    days_remaining = max(1, (last_day_of_month - today).days + 1)

    remaining_budget = monthly_salary - monthly_expenses
    daily_spending_limit = remaining_budget / days_remaining if days_remaining > 0 else 0

    return {
        'total_income': total_income,
        'total_expenses': total_expenses,
        'balance': total_income - total_expenses,
        'monthly_expenses': monthly_expenses,
        'monthly_salary': monthly_salary,
        'remaining_budget': remaining_budget,
        'daily_spending_limit': daily_spending_limit,
        'days_remaining': days_remaining,
        'category_expenses': category_expenses,
        'weekly_expenses': weekly_expenses,
    }


def generate_ai_advice(user_id):
    """Generate AI financial advice based on spending patterns"""
    summary = get_financial_summary(user_id)
    advice = []

    category_expenses = summary['category_expenses']
    category_dict = {item['category']: item['total'] for item in category_expenses}

    total_expenses = summary['total_expenses']
    monthly_salary = summary['monthly_salary']
    monthly_expenses = summary['monthly_expenses']
    balance = summary['balance']

    if monthly_salary > 0:
        expense_ratio = monthly_expenses / monthly_salary

        if expense_ratio > 0.9:
            advice.append({
                'type': 'danger',
                'icon': 'warning',
                'title': 'Critical Spending Alert',
                'message': (
                    f'You have spent {expense_ratio * 100:.1f}% of your monthly salary. '
                    'Consider immediate cost-cutting measures.'
                ),
            })
        elif expense_ratio > 0.75:
            advice.append({
                'type': 'warning',
                'icon': 'alert-triangle',
                'title': 'High Spending Warning',
                'message': (
                    f'You have used {expense_ratio * 100:.1f}% of your monthly budget. '
                    'Slow down on non-essential expenses.'
                ),
            })
        elif expense_ratio < 0.5:
            advice.append({
                'type': 'success',
                'icon': 'check-circle',
                'title': 'Excellent Savings!',
                'message': 'You are saving more than 50% of your income. Great financial discipline!',
            })

    if total_expenses > 0:
        for cat_name, amount in category_dict.items():
            percentage = (amount / total_expenses) * 100

            if percentage > 40:
                advice.append({
                    'type': 'warning',
                    'icon': 'trending-up',
                    'title': f'High {cat_name} Spending',
                    'message': (
                        f'{cat_name} accounts for {percentage:.1f}% of your total expenses. '
                        'Consider reducing spending in this category.'
                    ),
                })

    if balance < 0:
        advice.append({
            'type': 'danger',
            'icon': 'minus-circle',
            'title': 'Negative Balance',
            'message': (
                'Your expenses exceed your income. Create a strict budget and cut unnecessary spending immediately.'
            ),
        })
    elif balance > monthly_salary * 3:
        advice.append({
            'type': 'info',
            'icon': 'piggy-bank',
            'title': 'Investment Opportunity',
            'message': 'You have a healthy surplus. Consider investing in stocks, mutual funds, or retirement accounts.',
        })

    daily_limit = summary['daily_spending_limit']
    if daily_limit < 10 and monthly_salary > 0:
        advice.append({
            'type': 'warning',
            'icon': 'clock',
            'title': 'Low Daily Budget',
            'message': f'Your daily spending limit is only ₹{daily_limit:.2f} for the rest of the month.',
        })

    if not advice:
        advice.append({
            'type': 'info',
            'icon': 'info',
            'title': 'Steady Progress',
            'message': 'Your finances look stable. Continue monitoring your spending and look for small savings opportunities.',
        })

    return advice


# Routes
@app.route('/')
def index():
    """Home page - redirect to dashboard if logged in"""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    """User registration"""
    if not mongo_ready():
        flash('Database is unavailable. Set MONGODB_URI in .env', 'danger')
        return render_template('register.html')

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not username or not password:
            flash('Username and password are required', 'danger')
            return redirect(url_for('register'))

        if password != confirm_password:
            flash('Passwords do not match', 'danger')
            return redirect(url_for('register'))

        if len(password) < 6:
            flash('Password must be at least 6 characters', 'danger')
            return redirect(url_for('register'))

        hashed_password = generate_password_hash(password)
        email_value = email or None

        if mongo_db.users.find_one({'username': username}):
            flash('Username or email already exists', 'danger')
            return redirect(url_for('register'))

        if email_value and mongo_db.users.find_one({'email': email_value}):
            flash('Username or email already exists', 'danger')
            return redirect(url_for('register'))

        try:
            mongo_db.users.insert_one({
                'username': username,
                'email': email_value,
                'password': hashed_password,
                'monthly_salary': 0.0,
                'savings_goal': 0.0,
                'created_at': datetime.utcnow(),
            })
        except DuplicateKeyError:
            flash('Username or email already exists', 'danger')
            return redirect(url_for('register'))

        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login"""
    if not mongo_ready():
        flash('Database is unavailable. Set MONGODB_URI in .env', 'danger')
        return render_template('login.html')

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        doc = mongo_db.users.find_one({'username': username})
        user = user_doc_to_view(doc) if doc else None

        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash(f'Welcome back, {username}!', 'success')
            return redirect(url_for('dashboard'))

        flash('Invalid username or password', 'danger')

    return render_template('login.html')


@app.route('/logout')
def logout():
    """User logout"""
    session.clear()
    flash('You have been logged out', 'info')
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    """Main dashboard"""
    user_id = session['user_id']
    user = get_user_data(user_id)
    summary = get_financial_summary(user_id)
    recent_transactions = get_transactions(user_id, limit=5)
    ai_advice = generate_ai_advice(user_id)

    return render_template(
        'dashboard.html',
        user=user,
        summary=summary,
        transactions=recent_transactions,
        ai_advice=ai_advice,
    )


@app.route('/transactions')
@login_required
def transactions():
    """Transaction history page"""
    user_id = session['user_id']

    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    category = request.args.get('category', '')
    search = request.args.get('search', '')

    all_transactions = get_transactions(
        user_id,
        date_from=date_from or None,
        date_to=date_to or None,
        category=category or None,
        search=search or None,
    )

    categories = sorted(
        mongo_db.transactions.distinct('category', get_user_filter(user_id))
    )

    return render_template(
        'transactions.html',
        transactions=all_transactions,
        categories=categories,
        date_from=date_from,
        date_to=date_to,
        selected_category=category,
        search=search,
    )


@app.route('/add_transaction', methods=['POST'])
@login_required
def add_transaction():
    """Add new transaction"""
    user_id = session['user_id']

    transaction_type = request.form.get('type', 'expense')
    category = request.form.get('category', 'Other').strip() or 'Other'
    description = request.form.get('description', '').strip()

    try:
        amount = float(request.form.get('amount', 0))
    except ValueError:
        amount = 0.0

    date_value = request.form.get('date', '').strip()
    try:
        if date_value:
            datetime.strptime(date_value, '%Y-%m-%d')
        else:
            raise ValueError
    except ValueError:
        date_value = datetime.now().strftime('%Y-%m-%d')

    if amount <= 0:
        flash('Amount must be greater than 0', 'danger')
        return redirect(url_for('dashboard'))

    mongo_db.transactions.insert_one({
        'user_id': object_id(user_id) or user_id,
        'type': transaction_type,
        'category': category,
        'amount': round(amount, 2),
        'date': date_value,
        'description': description,
        'created_at': datetime.utcnow(),
    })

    flash('Transaction added successfully!', 'success')
    return redirect(url_for('dashboard'))


@app.route('/delete_transaction/<transaction_id>')
@login_required
def delete_transaction(transaction_id):
    """Delete a transaction"""
    user_id = session['user_id']
    oid = object_id(transaction_id)
    if oid is None:
        flash('Invalid transaction', 'danger')
        return redirect(url_for('transactions'))

    result = mongo_db.transactions.delete_one({'_id': oid, **get_user_filter(user_id)})
    if result.deleted_count:
        flash('Transaction deleted successfully!', 'success')
    else:
        flash('Transaction not found', 'warning')
    return redirect(url_for('transactions'))


@app.route('/update_salary', methods=['POST'])
@login_required
def update_salary():
    """Update monthly salary"""
    user_id = session['user_id']
    salary = float(request.form.get('monthly_salary', 0))

    user_oid = object_id(user_id)
    mongo_db.users.update_one({'_id': user_oid}, {'$set': {'monthly_salary': salary}})

    flash('Monthly salary updated!', 'success')
    return redirect(url_for('dashboard'))


@app.route('/api/chart_data')
@login_required
def chart_data():
    """API endpoint for chart data"""
    user_id = session['user_id']
    summary = get_financial_summary(user_id)

    categories = []
    amounts = []
    for item in summary['category_expenses']:
        categories.append(item['category'])
        amounts.append(item['total'])

    dates = []
    daily_amounts = []
    for i in range(7):
        day = (datetime.now() - timedelta(days=6 - i)).strftime('%Y-%m-%d')
        dates.append(datetime.strptime(day, '%Y-%m-%d').strftime('%a'))

        amount = 0
        for item in summary['weekly_expenses']:
            if item['date'] == day:
                amount = item['total']
                break
        daily_amounts.append(amount)

    return jsonify({
        'pie_chart': {
            'labels': categories,
            'data': amounts,
        },
        'bar_chart': {
            'labels': dates,
            'data': daily_amounts,
        },
        'summary': {
            'total_income': summary['total_income'],
            'total_expenses': summary['total_expenses'],
            'balance': summary['balance'],
            'monthly_expenses': summary['monthly_expenses'],
            'monthly_salary': summary['monthly_salary'],
            'remaining_budget': summary['remaining_budget'],
            'daily_spending_limit': summary['daily_spending_limit'],
        },
    })


@app.route('/api/ai_advice')
@login_required
def ai_advice_api():
    """API endpoint for AI advice"""
    user_id = session['user_id']
    advice = generate_ai_advice(user_id)
    return jsonify({'advice': advice})


@app.route('/savings_goals')
@login_required
def savings_goals():
    """Savings goals page"""
    user_id = session['user_id']

    cursor = mongo_db.savings_goals.find(get_user_filter(user_id)).sort('created_at', -1)
    goals = [goal_doc_to_view(g) for g in cursor]

    total_target = sum(goal['target_amount'] for goal in goals)
    total_saved = sum(goal['current_amount'] for goal in goals)
    active_goals = len(goals)
    completed_goals = sum(1 for goal in goals if goal['current_amount'] >= goal['target_amount'] > 0)

    return render_template(
        'savings_goals.html',
        goals=goals,
        total_target=total_target,
        total_saved=total_saved,
        active_goals=active_goals,
        completed_goals=completed_goals,
    )


@app.route('/add_savings_goal', methods=['POST'])
@login_required
def add_savings_goal():
    """Add new savings goal"""
    user_id = session['user_id']

    name = request.form.get('name', '').strip()
    deadline = request.form.get('deadline', '').strip()

    try:
        target_amount = float(request.form.get('target_amount', 0))
    except (TypeError, ValueError):
        target_amount = 0.0

    if not name or target_amount <= 0:
        flash('Please provide valid goal name and target amount', 'danger')
        return redirect(url_for('savings_goals'))

    mongo_db.savings_goals.insert_one({
        'user_id': object_id(user_id) or user_id,
        'name': name,
        'target_amount': round(target_amount, 2),
        'current_amount': 0.0,
        'deadline': deadline if deadline else None,
        'created_at': datetime.utcnow(),
    })

    flash('Savings goal added successfully!', 'success')
    return redirect(url_for('savings_goals'))


@app.route('/update_goal_progress/<goal_id>', methods=['POST'])
@login_required
def update_goal_progress(goal_id):
    """Update savings goal progress"""
    user_id = session['user_id']
    amount = float(request.form.get('amount', 0))

    oid = object_id(goal_id)
    if oid is None:
        flash('Invalid goal', 'danger')
        return redirect(url_for('savings_goals'))

    result = mongo_db.savings_goals.update_one(
        {'_id': oid, **get_user_filter(user_id)},
        {'$inc': {'current_amount': amount}},
    )
    if result.matched_count:
        flash('Goal progress updated!', 'success')
    else:
        flash('Goal not found', 'warning')
    return redirect(url_for('savings_goals'))


@app.route('/delete_goal/<goal_id>')
@login_required
def delete_goal(goal_id):
    """Delete a savings goal"""
    user_id = session['user_id']

    oid = object_id(goal_id)
    if oid is None:
        flash('Invalid goal', 'danger')
        return redirect(url_for('savings_goals'))

    result = mongo_db.savings_goals.delete_one({'_id': oid, **get_user_filter(user_id)})
    if result.deleted_count:
        flash('Goal deleted successfully!', 'success')
    else:
        flash('Goal not found', 'warning')
    return redirect(url_for('savings_goals'))


@app.route('/export_pdf')
@login_required
def export_pdf():
    """Export monthly report as PDF"""
    user_id = session['user_id']
    user = get_user_data(user_id)
    summary = get_financial_summary(user_id)

    current_month = datetime.now().strftime('%Y-%m')
    year_s, month_s = current_month.split('-')
    year_i = int(year_s)
    month_i = int(month_s)
    last_day = calendar.monthrange(year_i, month_i)[1]
    date_start = f'{current_month}-01'
    date_end = f'{current_month}-{last_day:02d}'

    cursor = mongo_db.transactions.find({
        **get_user_filter(user_id),
        'date': {'$gte': date_start, '$lte': date_end},
    }).sort([('date', -1)])
    month_transactions = [transaction_doc_to_view(t) for t in cursor]

    html_content = render_template(
        'pdf_report.html',
        user=user,
        summary=summary,
        transactions=month_transactions,
        month=datetime.now().strftime('%B %Y'),
    )

    response = make_response(html_content)
    response.headers['Content-Type'] = 'text/html'
    response.headers['Content-Disposition'] = f'attachment; filename=budget_report_{current_month}.html'

    return response


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    """User profile page"""
    user_id = session['user_id']
    user = get_user_data(user_id)
    user_oid = object_id(user_id)

    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')

        if email != (user['email'] or ''):
            if email and mongo_db.users.find_one({'email': email, '_id': {'$ne': user_oid}}):
                flash('Email already in use', 'danger')
            else:
                mongo_db.users.update_one(
                    {'_id': user_oid},
                    {'$set': {'email': email or None}},
                )
                flash('Email updated successfully!', 'success')
                user = get_user_data(user_id)

        if current_password and new_password:
            if check_password_hash(user['password'], current_password):
                hashed_password = generate_password_hash(new_password)
                mongo_db.users.update_one(
                    {'_id': user_oid},
                    {'$set': {'password': hashed_password}},
                )
                flash('Password updated successfully!', 'success')
                user = get_user_data(user_id)
            else:
                flash('Current password is incorrect', 'danger')

        return redirect(url_for('profile'))

    return render_template('profile.html', user=user)


@app.errorhandler(404)
def not_found(error):
    return render_template('error.html', error_code=404, error_message='Page not found'), 404


@app.errorhandler(500)
def server_error(error):
    return render_template('error.html', error_code=500, error_message='Internal server error'), 500


@app.template_filter('format_currency')
def format_currency(value):
    """Format number as currency"""
    if value is None:
        return '₹0.00'
    try:
        num = float(value)
    except (TypeError, ValueError):
        return '₹0.00'
    return f'₹{num:,.2f}'


@app.template_filter('format_date')
def format_date(value):
    """Format date string"""
    if isinstance(value, datetime):
        return value.strftime('%b %d, %Y')
    if isinstance(value, str) and value:
        try:
            dt = datetime.strptime(value, '%Y-%m-%d')
            return dt.strftime('%b %d, %Y')
        except ValueError:
            return value
    return ''


if __name__ == '__main__':
    init_mongo()
    app.run(debug=True, host='0.0.0.0', port=5000)
