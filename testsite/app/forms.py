
from flask import current_app
from flask.ext.wtf import (Form, TextField, PasswordField, Required, Email,
                           Length, Regexp, ValidationError, EqualTo)


class UniqueUser(object):
    def __init__(self, message="User exists"):
        self.message = message

    def __call__(self, form, field):
        if current_app.security.datastore.find_user(email=field.data):
            raise ValidationError(self.message)

validators = {
    'email': [
        Required(),
        Email(),
        UniqueUser(message='Email address is associated with '
                           'an existing account')
    ]
}


class RegisterForm(Form):
    email = TextField('Email', validators['email'])
