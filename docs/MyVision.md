What I am trying to build here is something totally differnt from whatever the conventional approah is for AI driven e-commerce.

We trying to build an agentic system that can be plugged into any e-commerce shop and that shop becomes agentic. How? thats what I am going to describe here.

So my approach is very simple. I do not want to expose conventional MCP tools that the AI can simply call and interact with our application. I feel thats the wrong way. One of the biggest hinderances I feel in that approach is that it obstructs the fidelity of the AI. Like as a human being when I am sufing a store I see the web for myself. I see a search bar and so many products around me. 

For Humans the HTML/CSS based approach is the best UI. But for Ai agents its the worst UI. I mean one way we can have a agents interact with the website is by giving it access to screenshots and HTML content and then hoping it will select the right HTML selectors or click on the correct cordinate. NOne of the which seem to anyway work properly.

Now one good way this can be handled is by having a rest-api endpoint for every single operation possible and then exposing the OpenAPI spec of that to that to the AI in a dyanmic basis and then it can write some custom code that can hit the correct endpoint and get things done. But the issue wiht that is obvious. Not every shop might have the perfect endpoints.

One way we could solve this is by creating a generic list of API endpoints that we feel everystore has and then release it as an SDK and then tell the stores to implement this contract or at the very least wire it to their code. Again not one click, and will be very messy for large shops.

Instead my approach is this. why not creat a website of AI agents itself. I mean nowhere is it written that the websites have to written in HTML or CSS or with JS. the only reason we write it with those is because the resulting UI from it is very easy for humans. And these cam at a time whe thinking that AI can surf websites for us was nothing short of science fiction.

So my plan is to have our own in house web server writeen in FastAPI just like any other web server, but for AI, and rather than serving html pages, it will server pages in an AI friendly fornat. (YOu can conisder JSON for now as that fornat for working later we can decide on how we can choose the best fornat, or even maybe tweak json.)

The whole thing will work like a nomal website, and the AI can surf the website as a normal human would. THe only difference the AI rater than seeig RAW HTML and boxes and all like we humans, would see the pages in say JSON (or another AI format) and then decide how to proceed. Like home page will have only search bar and some other options. AI can simualte click and types on those pages like we humans do with mouse, ai can do with its outputs in a certain fashion and then those will be executed and the website will open a new page like it does in normak with say the products matching the searched product and then the AI can decide which product to click to firther open and explore more and then finally after all of it it will come back with a response to final user.

Because even when we as humans click on a button in HTML UI it is also just a form of simulation where some code logic is written which says when clicked do this action. Its also made that way beause we humans enjoy working tactically clickng things ratehr than outputing strcuted response in text to click on buttons. But for AI the opposite it true. Cooridnate clicking is brittle but exact output in say a way we want it to output can simulate the same click or type etc. Kind of like how MCP tools are called. THe difference here its not calling a tool, its performing an action like a human does in a webite made for AI.

The idea is simple we will have a normalization layer whose job will be to take any form of data like csv SQL databses or anything and normalize them into a common format. This could be anything, I would prefer maybe storing in mongodb as its closer to JSON, what fornat we store in matters much less. What matters more is that we store all the data in a common format. Then that data will be used by our webiste to puplate the pages of our website for the AI agent. 

This is exactly same as how a normal website works. THe hTML css JS layer acts as a shell providing strcutre, but what actually goes into those and gets shown is the actual data. Here its just that the strcuture or the shell would be provided by our Ai friendly language.

Important point, real life data can be really messy and arbitary we cannot expect to hardocde mapping of columbs and all or only take certain fields as that reduce the fideltly of the AI by throwing away valuable info. Rather we take whatever is their as a whoole and figure out a way to deserialze this into our commond mongo format. BUt at the same time we will not use gen-ai here to say parse the input data and decide how to parse it. Atleast for now we will not use it as it would become slop.

The whole applicaiton will have:
    1. A nomralization layer class and a file for it. Inside of that the whole logic for notmalziing data.
    2. A Web Layer for AI, this will be a folder and inside of it will be the whole architecture of how a classical website is designed difference is in this case its a webserver inside a webserver kind of thing. THe outer one is what runs the whole applcaition including the human facing UI which we will have as well and the main server for AI. 
    3. A human facing UI folder inside of which create a proper chatbot and dashboard. We already have soe UI reference you an refine them to make it look polished.
    4. A Model Layer with logic for connecting to various ai models via API key. Use annyllm here.

IMP POINTS:
    do library driven development. Which means for every single line of code that you wrte you must first search the internet far and wide for if there is any Mature library that I can use for it, and if yes then use it can impleemnt it. 

    Never Never try to reinvent the wheel. I cannot emphasize this enoough never.
    Write very short and cocise code. Again I am telling the fewer lines of code per files and the fewer no of files you create the more point you will gain. If you over engineer you will be penalized. Dont write oevr complicated OOP archieture with 10s of fiels, but at the same time dont cra everytihng  into one. Infact acheve both I dont know how but hihgly optimizd and less evrbose code. For everything you wrte try to aheive it in under 2 3 lines max 10 lines.

Anotehr very imp thing, never hardcode anything into the spruce code. for env varibales use .env files, for tings which can be used mulitple places store them in some centralized file once, bt never ever hardcode things anywhere in code. DOnt use hacks anywhere, refer to docs online and stack overflow and try to wirte code liek a human. Add a comments over every funiton like a human would 1 2 lines explaining why this would work, and if there can be any issues.