from django.shortcuts import render, redirect
from django.contrib import messages
from .forms import ContactForm

def landing_view(request):
    return render(request, "core/landing.html")

def contact_view(request):
    if request.method == "POST":
        form = ContactForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Thank you for reaching out.")
            return redirect("contact")
    else:
        form = ContactForm()
    return render(request, "core/contact.html", {"form": form})


