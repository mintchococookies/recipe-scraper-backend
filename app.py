from flask import Flask, request
from flask_restx import Api, Resource, fields
import requests
from bs4 import BeautifulSoup
import re
from urllib.parse import urlparse
from copy import deepcopy
from flask_cors import CORS
import logging
import jwt
import datetime
import os
from functools import wraps
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

app = Flask(__name__)
CORS(app, origins=["*"])

authorizations = {
    'basicAuth': {
        'type': 'basic'
    }
}

api = Api(app, authorizations=authorizations)

# Define headers to simulate website access via browsers
headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/97.0.4692.71 Safari/537.36'
        }

load_dotenv()

app.config['SECRET_KEY'] = os.getenv('RECIPE_SCRAPER_SECRET_KEY', 'wheeee')  # for testing/dev

# ## LOGGING ######################################
# # Basic logging configuration
# logging.basicConfig(level=logging.INFO,
#                     format='%(asctime)s %(levelname)s %(name)s %(threadName)s : %(message)s')

# # Create a RotatingFileHandler
# handler = RotatingFileHandler('api.log', maxBytes=2000, backupCount=10)
# handler.setLevel(logging.INFO)
# formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(threadName)s : %(message)s')
# handler.setFormatter(formatter)

# # Add the RotatingFileHandler to the root logger
# logging.getLogger().addHandler(handler)
# ##################################################

# Define a model for the login request body (for Swagger UI clarity)
login_model = api.model('Login', {
    'username': fields.String(required=True, description='The username'),
    'password': fields.String(required=True, description='The password')
})

def generate_token(username):
    payload = {
        'username': username,
        'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=24)  # Token expiration time
    }
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')

def verify_credentials(username, password):
    # Replace with actual authentication mechanism (don't store passwords in plain text)
    expected_username = os.getenv('RECIPE_SCRAPER_USERNAME')
    expected_password = os.getenv('RECIPE_SCRAPER_PASSWORD')
    if username == expected_username and password == expected_password:
        return True
    return False

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', None)
        # logging.info(f"Received Authorization header: {token}")
        if not token:
            return {'message': 'Missing authorization token'}, 401

        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            current_user = data['username']
        except jwt.DecodeError:
            return {'message': 'Invalid token'}, 401

        return f(current_user, *args, **kwargs)

    return decorated

@api.route('/login')
class UserLogin(Resource):
    @api.expect(login_model)  # Optional for Swagger UI clarity
    def post(self):
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')

        if not username or not password:
            return {'message': 'Missing username or password'}, 401

        if not verify_credentials(username, password):
            return {'message': 'Invalid credentials'}, 401

        token = generate_token(username)
        return {'token': token}

# Define models for the input payloads
recipe_url_model = api.model('RecipeURL', {
    'recipe_url': fields.String(required=True, description='URL of the recipe')
})

unit_type_model = api.model('UnitType', {
    'unit_type': fields.String(required=True, description='Either "si" or "metric" ')
})

serving_size_model = api.model('ServingSize', {
    'serving_size': fields.String(required=True, description='Numeric value')
})

# Define some cooking action words to help locate the recipe in some websites where the recipe isn't labelled
cooking_action_words = [
    'heat', 'preheat', 'saute', 'stir', 'simmer', 'remove', 'serve', 'garnish', 
    'pour', 'mix', 'bake', 'grill', 'boil', 'chop', 'slice', 'dice', 'cool', 
    'prepare', 'melt', 'transfer', 'refrigerate', 'reheat', 'arrange', 'whisk', 'blend', 'fry',
    'marinate', 'combine', 'drizzle', 'sprinkle', 'toss', 'fold', 'cover', 'let stand',
    'mix together', 'beat', 'brush', 'shape', 'spray', 'roll', 'cut', 'spread', 'dip',
    'top with', 'squeeze', 'shake', 'divide', 'whip', 'knead', 'grate', 'baste', 'pound', 'set', 'mash', 'stir',
    'dry', 'wait', 'cool', 'season', 'start', 'cook'
]

common_units = [
    "grams", "gram", "milliliters", "milliliter", "centimeter", "centimeter", "kilograms", "kilogram", 
    "cups", "cup", "tablespoons", "tablespoon", "teaspoons", "teaspoon",
    "pounds", "pound", "ounces", "ounce", "grams", "gram", "tsp .", "tb .", "lb",
    "cloves", "clove", "can", "tin", "jar"
]

to_si_conversion = {
'cups': {'ml': 236.588, 'g': 125.39},
'cup': {'ml': 236.588, 'g': 125.39},
'lb': {'g': 453.592},
'oz': {'g': 28.3495}
} 

to_metric_conversion = {
'ml': {'cups': 0.00422675, 'cup': 0.00422675},
'l': {'cups': 4.22675, 'cup': 4.22675},
'g': {'oz': 0.03527396, 'cup': 0.007975, 'cups': 0.007975, 'lb': 1}
}

liquids = ["water", "oil", "milk", "honey"]
solids = ["flour", "pepper", "salt"]

# Global variables
global ingredients
global servings 
global ingredients_pre_conversion
global converted
global requested_serving_size
global original_unit_type
global unit_type

ingredients = None
servings = None
ingredients_pre_conversion = None
converted = False
requested_serving_size = None
original_unit_type = None
unit_type = None

# Function to extract the recipe steps when there's some labelling (id/class) on the html elements that indicates its the recipe
def extract_recipe_steps_labelled(soup):
    logging.info("DEBUG: extract_recipe_steps_labelled")
    recipe_steps = []
    step_positions = []
    extracted_steps = set()  

    recipe_steps_html = soup.find_all('li', id=re.compile(r'.*(instruction|direction|step).*', re.I))
    recipe_steps_html += soup.find_all(['p', 'li'], {'class': re.compile(r'instruction|direction|step', re.I)} )
    recipe_steps_html = [step.get_text(strip=True) for step in recipe_steps_html]
    has_cooking_related_words = any(any(word in step.lower() for word in cooking_action_words) for step in recipe_steps_html)
    if has_cooking_related_words:
        for element in recipe_steps_html:
            text = element.strip()
            if text.strip() not in extracted_steps:
                recipe_steps.append(text.strip())
                step_positions.append(recipe_steps_html.index(element))
                extracted_steps.add(text.strip())
    return recipe_steps

# Function to extract the recipe steps when there is no labelling (id/class) on the html elements at all to indicate that its the recipe
def extract_recipe_steps_manual(soup):
    recipe_steps = []

    # DOM traversal starting from any heading containing directions, instructions, method, or how to make
    if not recipe_steps:
        logging.info("DEBUG: extract_recipe_steps_manual")
        headers = soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
        target_headers = [header for header in headers if any(keyword.lower() in header.text.lower() for keyword in ['directions', 'instructions', 'method', 'how to make'])]

        for header in target_headers:
            current_element = header.parent
            while current_element:
                ol_element = current_element.find('ol')
                if ol_element:
                    for li in ol_element.find_all('li'):
                        step_text = li.get_text(strip=True)
                        if step_text not in recipe_steps:
                            recipe_steps.append(step_text)
                    break
                current_element = current_element.find_next()
    
    return recipe_steps

def extract_recipe_steps(soup):
    # Try to find the recipe by the class/id labels
    recipe_steps = extract_recipe_steps_labelled(soup)

    # If there's no labelling found
    if not recipe_steps:
        recipe_steps = extract_recipe_steps_manual(soup)

    return recipe_steps

def extract_ingredients(soup):
    ingredients = []

    # Found id or class labels for the ingredient li
    ingredients_html = [ingredient.text.strip() + " " for ingredient in soup.find_all('li', id=re.compile(r'.*(ingredient).*', re.I))]
    ingredients_html += [" ".join(ingredient.text.split()) for ingredient in soup.find_all(['p', 'li'], {'class': re.compile(r'ingredient', re.I)})]
    if ingredients_html:
        logging.info("DEBUG: method 1 ingredients")
        return sorted(set(ingredients_html))

    # Found ingredients list (ol/ul) but li is not labelled
    elif not ingredients_html:
        for element in soup.find_all(['ol', 'ul', 'div'], {'class': re.compile(r'ingredient', re.I)}):
            logging.info("DEBUG: method 2 ingredients")
            for item in element.find_all('li'):  # Find all list items within the <ol> or <ul>
                ingredient_text = item.get_text(strip=True)
                if ingredient_text not in ingredients:
                    ingredients.append(ingredient_text)  # Append each list item to the ingredients list

    # Manually search for what looks like ingredients (current limitation is if the ingredients list totally got no labelling anywhere in the whole page then cannot)
    if not ingredients:
        logging.info("DEBUG: method 3 ingredients")
        headers = soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
        target_headers = [header for header in headers if any(keyword.lower() in header.text.lower() for keyword in ['ingredients'])]

        for header in target_headers:
            current_element = header.parent    
            while current_element:
                ol_element = current_element.find('ul')
                if ol_element:
                    for li in ol_element.find_all('li'):
                        ingredient_text = li.get_text(strip=True)
                        if ingredient_text not in ingredients:
                            ingredients.append(ingredient_text)
                    break

                current_element = current_element.find_next()

    return ingredients

def extract_recipe_name(soup, recipe_url):
    # Parse the URL to extract the recipe name
    recipe_url = recipe_url[:-1] if recipe_url.endswith('/') else recipe_url
    parsed_url = urlparse(recipe_url)
    path_components = parsed_url.path.split('/')
    recipe_name_from_url = path_components[-1].replace('-', ' ').title()
    recipe_name_from_url = recipe_name_from_url.split('.')
    recipe_name_from_url = recipe_name_from_url[0]

    # labelled with ID or class
    title_html = [title.text.strip() + " " for title in soup.find_all(['h1', 'h2'], {'id': re.compile(r'.*(title|heading).*', re.I)})]
    title_html += [title.text.strip() + " " for title in soup.find_all(['h1', 'h2'], {'class': re.compile(r'.*(title|heading).*', re.I)})]
    recipe_name_list = [item for item in title_html if item.strip().istitle()]

    # compare which title/heading is most similar to the url cause the url usually has the recipe name in it
    for item in recipe_name_list:
        words1 = set(item.lower().split())
        words2 = set(recipe_name_from_url.lower().split())
        intersection = words1.intersection(words2)
        similarity = len(intersection) / max(len(words1), len(words2))
        if similarity >= 0.3:
            return item.strip()
    
    return recipe_name_from_url.strip()

# FUNCTION TO GET THE SERVING SIZE OF THE RECIPE ON THE WEBSITE
def get_serving_size(soup):
    servings = None
    elements = soup.find_all(['p', 'span', 'em', 'div'])
    target_elements = [element for element in elements if any(keyword.lower() in element.text.lower() for keyword in ['serves', 'servings', 'yield', 'serving'])]

    for element in target_elements:
        numbers = re.findall(r'\d+', element.get_text())
        if numbers:
            text = element.get_text()
            # check if the serving number is inside the same element as the word servings or yield etc
            match = re.search(r'(?:Yields:|Serves:|Servings:|Yield:|Serving:)\s*(.+)', text, re.IGNORECASE)
            if match:
                text = match.group(1)
                text = text.split(',') # split it just in case it comes together with the prep time etc
                # if after splitting its still very long, then split it again by spaces cause it probably still contains some other irrelevant information
                if len(text[0]) > 12:
                    text[0] = text[0].split(' ', 1)
                    return text[0][0]
                else:
                    return text[0]
            # if not, check whether it is at the same level in the DOM structure (for both this and next method, extract a digit once its found)
            else:
                current_element = element.find_next()
                while current_element:
                    numbers = re.findall(r'\d+', current_element.get_text())
                    if numbers and len(current_element.get_text()) < 10:
                        servings = int(list(filter(str.isdigit, current_element.get_text()))[0])
                        break
                    current_element = current_element.find_next()
                if servings is not None:
                    break
                # if also not, then check the next elements within the same parent
                else:
                    current_element = element.parent
                    while current_element:
                        numbers = re.findall(r'\d+', current_element.get_text())
                        if numbers and len(current_element.get_text()) < 10:
                            servings = int(list(filter(str.isdigit, current_element.get_text()))[0])
                            break
                        current_element = current_element.find_next()
                    if servings is not None:
                        break
    return servings

def postprocess_list(lst):
    if lst:
        return [re.sub(r'^\s*▢\s*', '', item) for item in lst]
    else:
        return None

def postprocess_text(txt):
    return txt.strip()

def standardize_units(ingredients):
    unit_mapping = {'g': 'g', 'gram': 'grams', 'g': 'grams', 'lb': 'lb', 'pound': 'lb', 'pounds': 'lb', 'kg': 'kg', 'kilogram': 'kg', 
                    'kilograms': 'kg', 'oz': 'oz', 'ounce': 'oz', 'ounces': 'oz', 'mg': 'mg', 'milligram': 'mg', 'milligrams': 'mg', 
                    'l': 'l', 'liter': 'l', 'liters': 'l', 'ml': 'ml', 'milliliter': 'ml', 'milliliters': 'ml', 'tsp': 'tsp', 
                    'teaspoon': 'tsp', 'teaspoons': 'tsp', 'tbsp': 'tbsp', 'tablespoon': 'tbsp', 'tablespoons': 'tbsp'}
    
    def clean_unit(unit):
        # Remove any trailing periods and extra spaces
        if unit:
            unit = re.sub(r'\s*\.\s*', '', unit)
            unit = unit.strip().lower()
        return unit
    
    for ingredient in ingredients:
        unit = clean_unit(ingredient[1])
        if ingredient and unit in unit_mapping:
            standardized_unit = unit_mapping[unit]
            ingredient[1] = standardized_unit
        else:
            pass

    return ingredients

# FUNCTIONS TO POSTPROCESS THE INGREDIENTS LIST
def extract_units(ingredients):
    parsed_ingredients = []
    for ingredient in ingredients:
        match = re.match(r'^((?:\d+\s*)?(?:\d*½|\d*¼|\d*[¾¾]|\d*⅛|\d*⅔|\d+\s*[/–-]|to\s*\d+)?[\s\d/–-]*)?[\s]*(?:([a-zA-Z]+)\b)?[\s]*(.*)$', ingredient)
        quantity, unit, name = match.groups()

        if '-' in str(quantity):
            quantity = str(quantity).replace(" ", "")
        elif 'to' in str(quantity):
            quantity = str(quantity).replace(" ", "")
            quantity = quantity.split('to')
            quantity = '-'.join(quantity)
        else:
            quantity = quantity.strip() if quantity else None
            quantity = quantity.replace("½", "1/2").replace("¼", "1/4").replace("¾", "3/4").replace("⅛", "1/8").replace("⅔", "2/3") if quantity else None
        
        if unit:
            modified_unit = re.split(r'^({})'.format('|'.join(common_units)), unit)
            unit = modified_unit[1] if len(modified_unit) > 1 else modified_unit[0]
            name = modified_unit[2] + " " + name if len(modified_unit) > 1 else name

        else:
            logging.info("DEBUG: Unit is NoneType:", name)
        
        if unit and unit.lower() not in common_units:
            name = unit + " " + name
            unit = None
        
        parsed_ingredients.append([quantity, unit, name.strip()])

    standardized_ingredients = standardize_units(parsed_ingredients)
    ingredients_pre_conversion = deepcopy(standardized_ingredients)
    
    if any(i[1] in to_si_conversion for i in ingredients_pre_conversion):
        original_unit_type = "metric"
    if any(i[1] in to_metric_conversion for i in ingredients_pre_conversion):
        original_unit_type = "si"

    return standardized_ingredients, original_unit_type, ingredients_pre_conversion

def calculate_servings(ingredients, servings, requested_serving_size):
    for ingredient in ingredients:
        quantity = ingredient[0]
        if not quantity:
            continue

        if '-' in str(quantity):
            quantity = str(quantity).replace(" ", "")
            quantity = quantity.split('-')
        elif 'to' in str(quantity):
            quantity = str(quantity).replace(" ", "")
            quantity = quantity.split('to')

        def convert_fraction(q):
            q = str(q).replace("1/2", "0.5").replace("1/4", "0.25").replace("3/4", "0.75").replace("1/8", "0.125").replace("2/3", "0.667")
            return q

        def adjust_quantity(q):
            q = sum(float(num_str) for num_str in q.split(" "))
            base_quantity = q / float(servings)
            temp_quantity = round((base_quantity * requested_serving_size), 3)
            temp_quantity = str(temp_quantity).replace("0.5", "1/2").replace("0.25", "1/4").replace("0.75", "3/4").replace("0.125", "1/8").replace("0.66", "2/3")
            temp_quantity = temp_quantity.replace(".5", " 1/2").replace(".25", " 1/4").replace(".75", " 3/4").replace(".125", " 1/8").replace(".66", " 2/3")
            return temp_quantity[:-2] if temp_quantity.endswith(".0") else temp_quantity

        if isinstance(quantity, list):
            new_quantity = [adjust_quantity(convert_fraction(q)) for q in quantity]
            ingredient[0] = '-'.join(new_quantity)
        else:
            ingredient[0] = adjust_quantity(convert_fraction(quantity))

    return ingredients

def convert_units(ingredients, unit_type, requested_serving_size, servings, original_unit_type, ingredients_pre_conversion):
    def convert_large_vals(converted_unit, converted_q):
        # if the cup value is super small, change to teaspoons (1 cup = 48 teaspoons)
        # if teaspoons is too much, change to tablespoons
        if converted_unit == "cups" and converted_q < 0.1:
            converted_unit = "tsp"
            converted_q *= 48

            if converted_q >= 3:
                converted_unit = "tbsp"
                converted_q /= 3 # 1 tablespoon = 3 teaspoons

        # if oz is greater than 32, change to pounds (1lb = 16oz)
        if converted_unit == "oz" and converted_q >= 32:
            converted_q /= 16 
            converted_unit = "lb"
        return converted_unit, converted_q

    # check if ingredients is populated or not first
    if not ingredients:
        return None
    
    conversion_dict = to_si_conversion if unit_type == "si" else to_metric_conversion
    
    # if they want to convert back to the original unit, must maintain the original units
    if original_unit_type == unit_type:
        if requested_serving_size is None or requested_serving_size == servings:
            logging.info("DEBUG: Conversion method 1")
            return ingredients_pre_conversion
        else:
            logging.info("DEBUG: Conversion method 2")
            ingredients = calculate_servings(deepcopy(ingredients_pre_conversion), servings, requested_serving_size)
            return ingredients
            
    # only do the conversion if they want a different unit
    else:
        for ingredient in ingredients:
            convert_to = None
            quantity = ingredient[0]
            if quantity: 
                # handle cases where the quantity is a range
                if '-' in str(quantity):
                    quantity = quantity.replace(" ", "")
                    quantity = quantity.split('-')
                elif 'to' in str(quantity):
                    quantity = quantity.replace(" ", "")
                    quantity = quantity.split('to')
                else:
                    # convert fractions like 3 1/4 to a whole number i.e. 3.25
                    quantity = str(quantity).replace("1/2", "0.5").replace("1/4", "0.25").replace("3/4", "0.75").replace("1/8", "0.125").replace("2/3", "0.667")
                    quantity_parts = quantity.split(" ")
                    quantity = sum(float(num_str) for num_str in quantity_parts)
                    
            unit = ingredient[1]
            name = ingredient[2]

            if quantity and unit and unit in [key for key, value in conversion_dict.items()]:
                # convert liquids from cups/tsp/tbsp to ml and solids to g
                if unit in ["cup", "cups", "tsp", "tbsp"]:
                    if any(liquid in name for liquid in liquids):
                        convert_to = "ml" 
                    else:
                        convert_to = "g"
                # convert grams of flour to cups of flour and not oz of flour
                if any(solid in name for solid in solids):
                    convert_to = "cups" if unit == "g" else "oz"
                converted_unit = convert_to if convert_to else [key for key, value in conversion_dict[unit].items()][0]
                
                # handle cases where the quantity is a range
                if isinstance(quantity, list):
                    converted_quantities = []
                    for q in quantity:
                        converted_q = float(q) * conversion_dict[unit][converted_unit]
                        converted_unit, converted_q = convert_large_vals(converted_unit, converted_q)
                        temp = round(converted_q, 2)
                        converted_quantities.append(str(temp))
                    converted_quantity = '-'.join(converted_quantities)
                    ingredient[0] = converted_quantity
                # handle normal single number quantities
                else:
                    converted_quantity = float(quantity) * conversion_dict[unit][converted_unit]
                    converted_unit, converted_quantity = convert_large_vals(converted_unit, converted_quantity)
                    ingredient[0] = round(converted_quantity, 2)
                ingredient[1] = converted_unit
        logging.info("DEBUG: Conversion method 3")

    return ingredients

# ============= APIs =============
@api.route('/convert-recipe-units')
class ConvertUnits(Resource):
    @api.doc(description="Convert between SI and metric units")
    @api.doc(security='basicAuth')
    @api.doc(params={'Authorization': {'in': 'header', 'description': 'Bearer <JWT token>', 'type': 'string'}})
    @api.expect(unit_type_model)
    @token_required
    def post(self, current_user):
        global ingredients
        global unit_type
        global requested_serving_size
        global servings
        global original_unit_type
        global ingredients_pre_conversion
        
        data = request.get_json()
        unit_type = data.get('unit_type')

        return convert_units(ingredients, unit_type, requested_serving_size, servings, original_unit_type, ingredients_pre_conversion)

@api.route('/calculate-serving-ingredients')
class MultiplyServingSize(Resource):
    @api.doc(description="Calculate the amount of ingredients based on the serving size wanted")
    @api.expect(serving_size_model)
    @api.doc(security='basicAuth')
    @api.doc(params={'Authorization': {'in': 'header', 'description': 'Bearer <JWT token>', 'type': 'string'}})
    @token_required
    def post(self, current_user):
        global servings
        global requested_serving_size
        global ingredients
        global unit_type
        global original_unit_type
        global ingredients_pre_conversion

        data = request.get_json()
        requested_serving_size = float(data.get('serving_size'))

        if not servings:
            return None
        
        # clean up the servings numbers
        servings = float(re.search("\\d+", str(servings))[0])

        if original_unit_type == unit_type:
            ingredients = calculate_servings(deepcopy(ingredients_pre_conversion), servings, requested_serving_size)
        else:
            temp = convert_units(deepcopy(ingredients_pre_conversion), unit_type, requested_serving_size, servings, original_unit_type, ingredients_pre_conversion)
            ingredients = calculate_servings(temp, servings, requested_serving_size)
        return ingredients

@api.route('/scrape-recipe-steps')
class ScrapeRecipeSteps(Resource):
    @api.doc(description="Recipe steps scraping")
    @api.expect(recipe_url_model)
    @api.doc(security='basicAuth')
    @api.doc(params={'Authorization': {'in': 'header', 'description': 'Bearer <JWT token>', 'type': 'string'}})
    @token_required
    def post(self, current_user):
        global ingredients
        global servings
        global original_unit_type
        global ingredients_pre_conversion

        data = request.get_json()
        recipe_url = data.get('recipe_url')
        
        try:
            response = requests.get(recipe_url, headers=headers)
            response.raise_for_status()  
        except requests.RequestException as e:
            return {'error': 'Failed to fetch recipe data: {}'.format(str(e))}, 500
        
        soup = BeautifulSoup(response.content, 'html.parser')

        recipe_name = postprocess_text(extract_recipe_name(soup, recipe_url))
        recipe_steps = postprocess_list(extract_recipe_steps(soup))
        ingredients = postprocess_list(extract_ingredients(soup))
        if ingredients:
            ingredients, original_unit_type, ingredients_pre_conversion = extract_units(ingredients)
            servings = get_serving_size(soup)
     
        if recipe_name and recipe_steps and ingredients and servings:
            return {'recipe_url': recipe_url, 'recipe_name': recipe_name, 'recipe_steps': recipe_steps, 'ingredients': ingredients, 'servings': servings, 'original_unit_type': original_unit_type}, 200
        else:
            return {"error": "Oops! We encountered a hiccup while trying to extract the recipe from this website. It seems its structure is quite unique and our system is having trouble with it. We're continuously working on improvements though! Thank you for your patience and support. ^^"}     