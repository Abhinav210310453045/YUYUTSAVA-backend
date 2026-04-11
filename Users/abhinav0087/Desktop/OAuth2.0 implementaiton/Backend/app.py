import streamlit as st

# Initialize session state
if 'show_login' not in st.session_state:
    st.session_state.show_login = False
if 'show_logout' not in st.session_state:
    st.session_state.show_logout = False

# Define dialogs
@st.dialog("Login Successful")
def login_dialog():
    st.write("You are logged in")
    if st.button("OK"):
        st.session_state.show_login = False
        st.rerun()

@st.dialog("Logout Successful")
def logout_dialog():
    st.write("You are logged out")
    if st.button("OK"):
        st.session_state.show_logout = False
        st.rerun()

# Main UI
st.title("Simple Login Demo")

col1, col2 = st.columns(2)

with col1:
    if st.button("Login"):
        st.session_state.show_login = True
        st.rerun()

with col2:
    if st.button("Logout"):
        st.session_state.show_logout = True
        st.rerun()

# Show dialogs if triggered
if st.session_state.show_login:
    login_dialog()

if st.session_state.show_logout:
    logout_dialog()